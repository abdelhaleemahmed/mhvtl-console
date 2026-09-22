"""status, targets, backstores, export, unexport, remap, target, lun, acl, portal, service.

Exporting the library over iSCSI. Every verb is one call into services/iscsi,
where the IQN and device-path checks live.

`status` says which source it read. saveconfig.json describes what was
configured; the running tree is what is exported now, and after the SCSI modules
reload a pscsi backstore naming /dev/sgN does not come back - so the two can
disagree, and on this host they did.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser('iscsi', help='exporting the library over iSCSI')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    status = verbs.add_parser('status', help='service, targets and LUNs')
    status.set_defaults(handler=do_status)

    targets = verbs.add_parser('targets', help='every target and what it exports')
    targets.set_defaults(handler=do_targets)

    backstores = verbs.add_parser('backstores', help='every backstore')
    backstores.set_defaults(handler=do_backstores)

    export = verbs.add_parser('export', help="export a library's changer and drives")
    export.add_argument('library_id', type=int)
    export.add_argument('--iqn', help='target name; generated if omitted')
    export.add_argument('--initiator',
                        help='allow only this initiator instead of any')
    export.set_defaults(handler=do_export)

    unexport = verbs.add_parser(
        'unexport', help="stop exporting a library, devices and all")
    unexport.add_argument('library_id', type=int)
    unexport.add_argument('--iqn', help='target name; generated if omitted')
    unexport.add_argument('--keep-backstores', action='store_true',
                          help='delete the target but leave its backstores, '
                               'which is what deleting a target alone does')
    unexport.set_defaults(handler=do_unexport)

    remap = verbs.add_parser(
        'remap', help='repoint saved backstores at their devices by SCSI address')
    remap.add_argument('--dry-run', action='store_true',
                       help='say what would change and change nothing')
    remap.add_argument('--wait', type=float, default=0, metavar='SECONDS',
                       help='wait up to this long for every device to appear '
                            '(used at boot)')
    remap.add_argument('--apply', action='store_true',
                       help='also restart target.service so the running export '
                            'matches; drops any connected initiator')
    remap.set_defaults(handler=do_remap)

    target = verbs.add_parser('target', help='create or delete a target')
    target.add_argument('action', choices=['create', 'delete'])
    target.add_argument('iqn')
    target.set_defaults(handler=do_target)

    lun = verbs.add_parser('lun', help='map or unmap a backstore')
    lun.add_argument('action', choices=['create', 'delete'])
    lun.add_argument('iqn')
    lun.add_argument('name', help='backstore name (create) or LUN number (delete)')
    lun.add_argument('--plugin', default='pscsi')
    lun.set_defaults(handler=do_lun)

    acl = verbs.add_parser('acl', help='allow or remove an initiator')
    acl.add_argument('action', choices=['create', 'delete'])
    acl.add_argument('iqn')
    acl.add_argument('initiator')
    acl.set_defaults(handler=do_acl)

    portal = verbs.add_parser('portal', help='listen on, or stop listening on, an address')
    portal.add_argument('action', choices=['create', 'delete'])
    portal.add_argument('iqn')
    portal.add_argument('--ip', default='0.0.0.0')
    portal.add_argument('--port', type=int, default=3260)
    portal.set_defaults(handler=do_portal)

    service = verbs.add_parser('service', help='control target.service')
    service.add_argument('action', choices=['start', 'stop', 'restart',
                                            'enable', 'disable'])
    service.set_defaults(handler=do_service)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service():
    from apps.libraries.services.iscsi import IscsiService
    return IscsiService()


def do_status(args) -> int:
    result = _service().status()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    data = result.data
    output.pairs({
        'service': 'running' if data['service']['running'] else 'stopped',
        'enabled': data['service']['enabled'],
        'targets': len(data['targets']),
        'backstores': len(data['backstores']),
        'luns': data['lun_count'],
        'saved': data['config_saved'],
        'source': data['source'],
    }, labels={'service': 'target.service', 'saved': 'config saved',
               'source': 'read from'})
    return output.EXIT_OK


def do_targets(args) -> int:
    result = _service().targets()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    rows = []
    for target in result.data['targets']:
        for tpg in target.get('tpgs', []):
            rows.append({'iqn': target['iqn'], 'tpg': tpg.get('tag'),
                         'luns': len(tpg.get('luns', [])),
                         'acls': len(tpg.get('acls', [])),
                         'open': tpg.get('generate_node_acls')})
    if not rows:
        print('No iSCSI targets are configured.')
        return output.EXIT_OK
    output.table(rows, columns=['iqn', 'tpg', 'luns', 'acls', 'open'],
                 headers=['iqn', 'tpg', 'luns', 'acls', 'any initiator'])
    return output.EXIT_OK


def do_backstores(args) -> int:
    result = _service().backstores()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    if not result.data['backstores']:
        print('No backstores are configured.')
        return output.EXIT_OK
    output.table(result.data['backstores'],
                 columns=['name', 'plugin', 'device_path'])
    return output.EXIT_OK


def do_unexport(args) -> int:
    """Stop exporting a library: its target, and the devices it exported.

    The inverse of `export`. Deleting the target alone leaves the pscsi
    backstores behind, holding the /dev/sg node each was made with - see
    workflow.unexport_library for why that matters.
    """
    privileges.require_write_access('unexporting a library')
    result = _service().unexport_library(
        args.library_id, iqn=args.iqn,
        remove_backstores=not args.keep_backstores)

    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    print(result.message)
    for step in (result.data or {}).get('steps', []):
        mark = 'ok  ' if step['ok'] else 'FAIL'
        print(f'  {mark} {step["step"]}')
    return output.EXIT_OK


def do_export(args) -> int:
    """Export every device of a library that the kernel can see."""
    privileges.require_write_access('exporting a library')
    from apps.libraries.services.scsi import mapping

    resolved = mapping.map_all(args.config_dir)
    from apps.libraries.services.config.service import ConfigService
    conf = ConfigService(args.config_dir).device_conf()
    if conf is None or args.library_id not in conf.libraries:
        return output.fail(f'library {args.library_id} is not in device.conf')

    devices = []
    changer = _generic(resolved['libraries'].get(args.library_id))
    if changer:
        devices.append({'name': f'lib{args.library_id}_changer',
                        'device_path': changer, 'type': 'changer'})
    for position, drive_id in enumerate(sorted(conf.drives_of(args.library_id))):
        node = _generic(resolved['drives'].get(drive_id))
        if node:
            devices.append({'name': f'lib{args.library_id}_drive{position}',
                            'device_path': node, 'type': 'tape'})

    if not devices:
        return output.fail(f'no device of library {args.library_id} is visible',
                           f'is vtllibrary@{args.library_id} running?')

    result = _service().export_library(
        args.library_id, devices, iqn=args.iqn,
        allow_all_initiators=not args.initiator,
        initiator_iqn=args.initiator)
    return output.result(result, as_json=args.json, quiet=args.quiet)


def _generic(device_path):
    """A pscsi backstore needs the /dev/sgN node; the map may hold /dev/stN."""
    if not device_path:
        return None
    if device_path.startswith('/dev/sg'):
        return device_path
    from apps.libraries.services.scsi import lsscsi
    for device in lsscsi.local():
        if device.device_path == device_path:
            return device.generic_path
    return None


def do_remap(args) -> int:
    """Fix saveconfig.json by address; optionally reload the running export.

    At boot mhvtl-iscsi-remap.service runs this with --wait and without
    --apply, before target.service reads the file. By hand, --apply restarts
    target.service, which is `targetctl clear` then `targetctl restore`.
    """
    if not args.dry_run:
        privileges.require_write_access('remapping iSCSI backstores')
    from apps.libraries.services.iscsi import remap

    result = remap.remap(dry_run=args.dry_run, wait=args.wait,
                         config_directory=args.config_dir)
    if args.json:
        output.emit_json(result.to_dict())
    elif not result.success:
        return output.fail(result.message, *result.errors)
    else:
        rows = [change for change in result.data['changes']]
        if rows:
            output.table(rows, columns=['name', 'saved', 'wanted', 'reason'],
                         headers=['backstore', 'saved', 'now', 'why'])
            print()
        if not args.quiet:
            print(result.message)

    if not result.success:
        return output.EXIT_FAILED
    if args.apply and not args.dry_run:
        from apps.libraries.services.console import units
        restarted = units.restart('target.service')
        if not restarted.ok:
            return output.fail('saveconfig.json was fixed but target.service did '
                               'not restart', restarted.output.strip()[:300])
        if not args.quiet and not args.json:
            print('target.service restarted; the running export now matches.')
    return output.EXIT_OK


def do_target(args) -> int:
    privileges.require_write_access(f'{args.action[:-1]}ing an iSCSI target')
    service = _service()
    call = service.create_target if args.action == 'create' else service.delete_target
    return output.result(call(args.iqn), as_json=args.json, quiet=args.quiet)


def do_lun(args) -> int:
    privileges.require_write_access('changing an iSCSI LUN')
    service = _service()
    if args.action == 'create':
        result = service.create_lun(args.iqn, args.plugin, args.name)
    else:
        if not args.name.isdigit():
            return output.fail('delete takes the LUN number', code=output.EXIT_USAGE)
        result = service.delete_lun(args.iqn, int(args.name))
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_acl(args) -> int:
    privileges.require_write_access('changing an iSCSI ACL')
    service = _service()
    call = service.create_acl if args.action == 'create' else service.delete_acl
    return output.result(call(args.iqn, args.initiator), as_json=args.json,
                         quiet=args.quiet)


def do_portal(args) -> int:
    privileges.require_write_access('changing an iSCSI portal')
    service = _service()
    call = (service.create_portal if args.action == 'create'
            else service.delete_portal)
    return output.result(call(args.iqn, args.ip, args.port), as_json=args.json,
                         quiet=args.quiet)


def do_service(args) -> int:
    privileges.require_write_access(f'the target.service {args.action}')
    return output.result(_service().service(args.action), as_json=args.json,
                         quiet=args.quiet)
