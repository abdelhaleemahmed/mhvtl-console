"""mount, unmount, move, online, offline, map, inventory.

Moving tapes. Every verb is one call into services/operations, which addresses
the robot through mtx and the daemons through vtlcmd.

Two things this noun exists to get right, both of which the web UI got wrong
before the refactor:

    A drive number is not a drive id. mtx numbers a library's drives from 0;
    device.conf numbers them globally, so library 20's first drive is 21 to
    device.conf and 0 to mtx. These verbs take the mtx number, because that is
    what an operator reads off `mhvtl status library`.

    vtlcmd takes the device.conf id as its queue id, not a derived index. The
    version this replaces computed `library_id // 10`, so every online, offline
    and MAP command addressed a queue that did not exist.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser('op', help='move tapes and control libraries')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    mount = verbs.add_parser('mount', help='load a tape from a slot into a drive')
    mount.add_argument('library_id', type=int)
    mount.add_argument('slot', type=int)
    mount.add_argument('drive', type=int, help='mtx drive number, from 0')
    mount.add_argument('--force', action='store_true',
                       help='mount even when the drive does not load this media')
    mount.set_defaults(handler=do_mount)

    unmount = verbs.add_parser('unmount', help='return a tape to a slot')
    unmount.add_argument('library_id', type=int)
    unmount.add_argument('drive', type=int, help='mtx drive number, from 0')
    unmount.add_argument('--slot', type=int,
                         help='slot to return it to; its own by default')
    unmount.set_defaults(handler=do_unmount)

    move = verbs.add_parser('move', help='move a tape between slots')
    move.add_argument('library_id', type=int)
    move.add_argument('from_slot', type=int)
    move.add_argument('to_slot', type=int)
    move.set_defaults(handler=do_move)

    online = verbs.add_parser('online', help='bring a library online')
    online.add_argument('library_id', type=int)
    online.set_defaults(handler=do_online)

    offline = verbs.add_parser('offline', help='take a library offline')
    offline.add_argument('library_id', type=int)
    offline.set_defaults(handler=do_offline)

    mapping = verbs.add_parser('map', help='the import/export port')
    mapping.add_argument('library_id', type=int)
    mapping.add_argument('action', choices=['open', 'close', 'load', 'list',
                                            'empty'])
    mapping.set_defaults(handler=do_map)

    inventory = verbs.add_parser(
        'inventory', help='have the robot re-read every barcode')
    inventory.add_argument('library_id', type=int)
    inventory.set_defaults(handler=do_inventory)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.operations.service import OperationsService
    return OperationsService(args.config_dir)


def do_mount(args) -> int:
    privileges.require_write_access('mounting a tape')
    return output.result(_service(args).mount(args.library_id, args.slot,
                                              args.drive, force=args.force),
                         as_json=args.json, quiet=args.quiet)


def do_unmount(args) -> int:
    privileges.require_write_access('unmounting a tape')
    return output.result(_service(args).unmount(args.library_id, args.drive,
                                                slot=args.slot),
                         as_json=args.json, quiet=args.quiet)


def do_move(args) -> int:
    privileges.require_write_access('moving a tape')
    return output.result(_service(args).move(args.library_id, args.from_slot,
                                             args.to_slot),
                         as_json=args.json, quiet=args.quiet)


def do_online(args) -> int:
    privileges.require_write_access('bringing a library online')
    return output.result(_service(args).online(args.library_id),
                         as_json=args.json, quiet=args.quiet)


def do_offline(args) -> int:
    privileges.require_write_access('taking a library offline')
    return output.result(_service(args).offline(args.library_id),
                         as_json=args.json, quiet=args.quiet)


def do_map(args) -> int:
    privileges.require_write_access(f'the MAP {args.action} command')
    return output.result(_service(args).map_command(args.library_id, args.action),
                         as_json=args.json, quiet=args.quiet)


def do_inventory(args) -> int:
    """Slower than a status read: the robot physically scans every slot."""
    privileges.require_write_access('inventorying a library')
    from apps.libraries.services.operations import mtx
    from apps.libraries.services.scsi import mapping

    device = mapping.device_for_library(args.library_id, config_dir=args.config_dir)
    if device is None:
        return output.fail(f'no changer for library {args.library_id}',
                           f'is vtllibrary@{args.library_id} running?')

    result = mtx.inventory(device)
    if not result.ok:
        return output.fail(f'inventory failed on library {args.library_id}',
                           result.output.strip()[:300])
    if not args.quiet:
        print(f'Library {args.library_id} inventory completed')
    return output.EXIT_OK
