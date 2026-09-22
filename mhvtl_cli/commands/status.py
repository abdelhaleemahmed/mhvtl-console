"""library, drive, dashboard, system.

Read-only, so none of it asks for privileges: if you can read the configuration
you can ask what state things are in. What each verb reads is worth knowing,
because they disagree in a way that is informative rather than a bug:

    status library   the robot, through mtx - what is physically where
    status drive     the drive daemon, through mt - what it thinks it holds
    status activity  what each drive is doing, through MHVTL's message queue
    status system    systemd and the kernel modules
    status dashboard everything the web overview shows, in one answer

`status activity` is the only one that answers during a backup: the kernel
holds the SCSI reservation for the initiator, so mt and mtx say "device busy",
while the drive daemon's own counters come over its message queue and are not
affected by it.

mtx is the authority on what is loaded where. A drive daemon's idea of its own
state can be stale after a restart, which is an MHVTL bug upstream, so when the
two disagree `status library` is the one to believe.
"""
from .. import output


def register(subparsers) -> None:
    parser = subparsers.add_parser('status', help='what state things are in')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    library = verbs.add_parser('library', help="a library's slots and drives")
    library.add_argument('library_id', type=int)
    library.set_defaults(handler=do_library)

    drive = verbs.add_parser('drive', help='what a drive reports')
    drive.add_argument('drive_id', type=int)
    drive.set_defaults(handler=do_drive)

    activity = verbs.add_parser('activity',
                                help='what each drive is doing right now')
    activity.add_argument('library_id', type=int)
    activity.add_argument('--settle', type=float, default=1.0, metavar='SECONDS',
                          help='how long to wait between the two readings that '
                               'say whether a drive is moving (default 1)')
    activity.set_defaults(handler=do_activity)

    system = verbs.add_parser('system', help='daemons, modules and disk')
    system.set_defaults(handler=do_system)

    dashboard = verbs.add_parser('dashboard',
                                 help='everything the web overview shows')
    dashboard.set_defaults(handler=do_dashboard)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def do_library(args) -> int:
    from apps.libraries.services.operations.service import OperationsService

    result = OperationsService(args.config_dir).status(args.library_id)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    data = result.data
    summary = data['summary']
    print(f'Library {args.library_id}: '
          f'{summary["full_slots"]}/{summary["total_slots"]} slots full, '
          f'{summary["loaded_drives"]}/{summary["total_drives"]} drives loaded')

    if data['drives']:
        print('\nDrives:')
        output.table(data['drives'], columns=['number', 'full', 'barcode',
                                              'slot_origin'],
                     headers=['drive', 'loaded', 'barcode', 'from slot'])

    full = [slot for slot in data['slots'] if slot['full']]
    if full:
        print(f'\nSlots holding a tape ({len(full)}):')
        output.table(full, columns=['number', 'barcode'],
                     headers=['slot', 'barcode'])
    return output.EXIT_OK


def do_drive(args) -> int:
    from apps.libraries.services.operations import mt
    from apps.libraries.services.scsi import mapping

    device = mapping.device_for_drive(args.drive_id, config_dir=args.config_dir)
    if device is None:
        return output.fail(
            f'no device for drive {args.drive_id}',
            'no SCSI device reports the address device.conf gives it; '
            'is vtltape@%s running?' % args.drive_id)

    state = mt.status(device)
    if args.json:
        output.emit_json({'drive_id': args.drive_id, **state.to_dict()})
        return output.EXIT_OK

    output.pairs({'drive': args.drive_id, 'device': device, **state.to_dict()},
                 keys=['drive', 'device', 'online', 'ready', 'has_medium',
                       'density_name', 'block_size', 'write_protected',
                       'at_bot', 'at_eot'],
                 labels={'has_medium': 'tape loaded', 'density_name': 'density',
                         'at_bot': 'at start of tape', 'at_eot': 'at end of tape',
                         'block_size': 'block size',
                         'write_protected': 'write protected'})
    return output.EXIT_OK


def do_activity(args) -> int:
    """What each drive is doing, in the words the library page uses.

    MHVTL counts totals rather than a rate, so this reads twice, `--settle`
    seconds apart, and reports a drive as writing when the second reading is
    larger. The decision and the wording are the service's - the same call the
    web page makes - so the two cannot drift apart.
    """
    from apps.libraries.services.drives import DriveService

    result = DriveService(args.config_dir).activity(args.library_id,
                                                    settle=args.settle)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    data = result.data
    print(f'Library {args.library_id}: {result.message}')
    if not data['drives']:
        return output.EXIT_OK

    print()
    output.table(data['drives'],
                 columns=['drive_id', 'state', 'barcode', 'detail'],
                 headers=['drive', 'state', 'barcode', 'counters'])
    return output.EXIT_OK


def do_system(args) -> int:
    from apps.libraries.services.console import modules, units

    state = units.status(args.config_dir)
    kernel = modules.summary()

    if args.json:
        output.emit_json({'units': state.to_dict() if hasattr(state, 'to_dict')
                          else vars(state),
                          'modules': kernel})
        return output.EXIT_OK

    from django.conf import settings

    output.pairs({
        'config': str(getattr(settings, 'MHVTL_CONFIG_DIR', '?')),
        'target': 'running' if state.target_active else 'stopped',
        'enabled': state.target_enabled,
        'backend': kernel['backend'],
        'libraries': f'{state.libraries_active}/{len(state.libraries)} running',
        'drives': f'{state.drives_active}/{len(state.drives)} running',
        'healthy': state.healthy,
    }, labels={'target': 'mhvtl.target', 'config': 'reading'})

    if state.stale:
        print(f'\n{len(state.stale)} unit(s) systemd still knows about that '
              f'device.conf no longer declares:')
        for unit in state.stale:
            print(f'  {unit}')
        print('  (they stay loaded until the machine reboots; '
              '"mhvtl library orphans --clean" removes them)')
    return output.EXIT_OK


def do_dashboard(args) -> int:
    from apps.libraries.services.dashboard import service as dashboard

    summary = dashboard.get_dashboard_summary()
    if args.json:
        output.emit_json(summary)
        return output.EXIT_OK

    for name in ('service', 'libraries', 'media', 'storage'):
        section = summary.get(name, {})
        if not section.get('ok'):
            print(f'{name}: unavailable - {section.get("error", "unknown")}')
            continue
        print(f'{name}:')
        data = section.get('data') or {}
        output.pairs({k: v for k, v in data.items()
                      if not isinstance(v, (list, dict))})
        print()

    alerts = summary.get('alerts') or []
    if alerts:
        print('Alerts:')
        for alert in alerts:
            print(f'  [{alert["level"]}] {alert["message"]}')

    return output.EXIT_FAILED if summary.get('degraded') else output.EXIT_OK
