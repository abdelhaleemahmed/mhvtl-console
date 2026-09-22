"""list, show, add, remove.

Argument handling and printing only; every verb is one call into
services/drives.

Worth knowing about add and remove: a drive is a device.conf record *and* a
`Drive N:` line in the library's contents file, and the daemons read both. The
service writes both together; these verbs would be a good place to get that
wrong, so they do nothing but pass arguments through.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser('drive', help='tape drives')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='drives, all or in one library')
    listing.add_argument('library_id', type=int, nargs='?',
                         help='limit to one library')
    listing.set_defaults(handler=do_list)

    show = verbs.add_parser('show', help='one drive in detail')
    show.add_argument('drive_id', type=int)
    show.set_defaults(handler=do_show)

    add = verbs.add_parser('add', help='add a drive to a library')
    add.add_argument('library_id', type=int)
    add.add_argument('--vendor', help='override the library profile')
    add.add_argument('--model', dest='product', help='override the library profile')
    add.add_argument('--serial')
    add.add_argument('--no-restart', action='store_true',
                     help='write device.conf but leave the daemons alone; the '
                          'drive is invisible until the library restarts')
    add.set_defaults(handler=do_add)

    remove = verbs.add_parser('remove', help='remove a drive')
    remove.add_argument('drive_id', type=int)
    remove.add_argument('--no-restart', action='store_true',
                        help='write device.conf but leave the daemons alone')
    remove.set_defaults(handler=do_remove)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.drives import DriveService
    return DriveService(args.config_dir)


def do_list(args) -> int:
    result = _service(args).list(args.library_id)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    drives = result.data['drives']
    if not drives:
        where = f' in library {args.library_id}' if args.library_id else ''
        print(f'No drives are configured{where}.')
        return output.EXIT_OK

    output.table(drives,
                 columns=['drive_id', 'library_id', 'slot', 'vendor', 'product',
                          'serial', 'target'],
                 headers=['id', 'library', 'slot', 'vendor', 'model', 'serial',
                          'target'])
    return output.EXIT_OK


def do_show(args) -> int:
    result = _service(args).get(args.drive_id)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    output.pairs(result.data['drive'])
    return output.EXIT_OK


def do_add(args) -> int:
    privileges.require_write_access('adding a drive')

    spec = {key: value for key, value in
            (('vendor', args.vendor), ('product', args.product),
             ('serial', args.serial)) if value}
    result = _service(args).add(args.library_id, spec or None,
                                restart=not args.no_restart)
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_remove(args) -> int:
    privileges.require_write_access('removing a drive')
    result = _service(args).remove(args.drive_id, restart=not args.no_restart)
    return output.result(result, as_json=args.json, quiet=args.quiet)
