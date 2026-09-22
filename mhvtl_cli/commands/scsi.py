"""devices, map.

What the kernel sees, and how device.conf's libraries and drives resolve to it.

`map` is the diagnostic that matters. A library whose address is in device.conf
but that no SCSI device reports is a library whose daemon has not started, and
every operation on it will fail with "could not find device". The map shows
that as a missing node rather than as a guess: device mapping matches on
CHANNEL/TARGET/LUN, never on position.
"""
from .. import output


def register(subparsers) -> None:
    parser = subparsers.add_parser('scsi', help='SCSI devices and their mapping')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    devices = verbs.add_parser('devices', help='changers and tape drives the kernel sees')
    devices.add_argument('--all', action='store_true',
                         help='include disks and everything else')
    devices.set_defaults(handler=do_devices)

    mapping = verbs.add_parser('map', help='which device each library and drive is')
    mapping.set_defaults(handler=do_map)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def do_devices(args) -> int:
    from apps.libraries.services.scsi import lsscsi

    found = lsscsi.discover()
    if not args.all:
        found = [device for device in found
                 if device.device_type in ('mediumx', 'tape')]
    rows = [device.to_dict() for device in found]

    if args.json:
        output.emit_json(rows)
        return output.EXIT_OK
    if not rows:
        print('No changers or tape drives are visible. Is mhvtl.target running?')
        return output.EXIT_OK

    output.table(rows, columns=['host', 'device_type', 'vendor', 'model',
                                'device_path', 'generic_path'],
                 headers=['address', 'type', 'vendor', 'model', 'device',
                          'generic'])
    return output.EXIT_OK


def do_map(args) -> int:
    from apps.libraries.services.scsi import mapping

    resolved = mapping.map_all(args.config_dir)
    if args.json:
        output.emit_json(resolved)
        return output.EXIT_OK

    rows = ([{'kind': 'library', 'id': key, 'device': value}
             for key, value in sorted(resolved['libraries'].items())] +
            [{'kind': 'drive', 'id': key, 'device': value}
             for key, value in sorted(resolved['drives'].items())])
    if not rows:
        return output.fail('device.conf declares nothing, or cannot be read')

    output.table(rows, columns=['kind', 'id', 'device'])
    missing = [row for row in rows if not row['device']]
    if missing:
        print(f'\n{len(missing)} declared device(s) are not visible to the kernel;'
              f' their daemons are probably not running.')
        return output.EXIT_FAILED
    return output.EXIT_OK
