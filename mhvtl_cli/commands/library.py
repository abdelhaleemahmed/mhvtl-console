"""list, show, create, update, delete, orphans, next-id.

Argument handling and printing only; every verb is one call into
services/libraries. Where a verb changes something it asks privileges first, so
the refusal names the group rather than arriving as a sudo error eight steps in.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser('library', help='libraries and their drives')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='every library in device.conf')
    listing.set_defaults(handler=do_list)

    show = verbs.add_parser('show', help='one library in detail')
    show.add_argument('library_id', type=int)
    show.set_defaults(handler=do_show)

    next_id = verbs.add_parser('next-id', help='the next free library id')
    next_id.set_defaults(handler=do_next_id)

    orphans = verbs.add_parser(
        'orphans', help='drives, files and units nothing declares any more')
    orphans.add_argument('--clean', action='store_true',
                         help='remove what is found')
    orphans.set_defaults(handler=do_orphans)

    slots = verbs.add_parser(
        'slots', help='how many empty slots a library has, or change it')
    slots.add_argument('library_id', type=int)
    group = slots.add_mutually_exclusive_group()
    group.add_argument('--empty', type=int,
                       help='set the number of empty slots')
    group.add_argument('--add', type=int,
                       help='add this many empty slots')
    slots.add_argument('--restart', action='store_true',
                       help='restart the library, so its robot sees the change')
    slots.set_defaults(handler=do_slots)

    create = verbs.add_parser('create', help='create a library and its drives')
    create.add_argument('--profile', required=True,
                        help='vendor profile, e.g. IBM, STK, SONY')
    create.add_argument('--id', type=int, dest='library_id',
                        help='library id; the next free one if omitted')
    create.add_argument('--drives', type=int, dest='num_drives',
                        help='how many drives')
    create.add_argument('--media-type', help='e.g. LTO8')
    create.add_argument('--tapes', type=int, dest='media_count',
                        help='how many cartridges to put in slots')
    create.add_argument('--empty-slots', type=int, dest='empty_slots',
                        help='slots to leave empty, for tapes added later '
                             "(the profile's default if omitted)")
    create.add_argument('--model', dest='library_model',
                        help="library model (product string); the profile's "
                             'default if omitted')
    create.add_argument('--drive-model', dest='drive_model',
                        help="drive model; the model's default if omitted")
    create.add_argument('--serial', help='unit serial number')
    create.add_argument('--no-start', action='store_true',
                        help='write the configuration but do not start the daemons')
    create.add_argument('--no-media', action='store_true',
                        help='do not create the tape files library_contents lists')
    create.add_argument('--dry-run', action='store_true',
                        help='print the device.conf text it would write, and stop')
    create.set_defaults(handler=do_create)

    delete = verbs.add_parser('delete', help='remove a library and its drives')
    delete.add_argument('library_id', type=int)
    delete.add_argument('--force', action='store_true',
                        help='delete even with a tape loaded in a drive')
    delete.add_argument('--remove-media', action='store_true',
                        help='also delete the tapes, which is not reversible')
    delete.set_defaults(handler=do_delete)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.libraries import LibraryService
    return LibraryService(args.config_dir)


# -- reading ---------------------------------------------------------------

def do_list(args) -> int:
    result = _service(args).list()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    libraries = result.data['libraries']
    if not libraries:
        print('No libraries are configured.')
        return output.EXIT_OK

    output.table(libraries,
                 columns=['library_id', 'model', 'serial', 'drives',
                          'slot_count', 'tape_count'],
                 headers=['id', 'model', 'serial', 'drives', 'slots', 'tapes'])
    return output.EXIT_OK


def do_show(args) -> int:
    result = _service(args).get(args.library_id)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    output.pairs(result.data['library'],
                 keys=['library_id', 'vendor', 'product', 'serial', 'channel',
                       'target', 'lun', 'naa', 'home_directory', 'drives',
                       'drive_ids', 'slot_count', 'tape_count'],
                 labels={'library_id': 'id', 'naa': 'NAA',
                         'slot_count': 'slots', 'tape_count': 'tapes'})
    return output.EXIT_OK


def do_next_id(args) -> int:
    result = _service(args).next_id()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    print(result.data['library_id'])
    return output.EXIT_OK


def do_orphans(args) -> int:
    from apps.libraries.services.libraries import orphans

    if args.clean:
        privileges.require_write_access('cleaning up orphans')
        return output.result(orphans.cleanup(config_directory=args.config_dir),
                             as_json=args.json, quiet=args.quiet)

    result = orphans.find_result(args.config_dir)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    found = result.data
    for label, key, columns in (
            ('Orphaned drives', 'orphaned_drives', ['drive_id', 'reason']),
            ('Orphaned files', 'orphaned_files', ['path', 'reason']),
            ('Orphaned units', 'orphaned_services', ['service', 'reason'])):
        if found[key]:
            print(f'\n{label}:')
            output.table(found[key], columns=columns)

    # Media is listed apart from the rest and never cleaned: these are tapes
    # with their data, and `tape adopt` puts one back into a library.
    if found.get('orphaned_media'):
        print('\nTapes on disk no library lists (data kept):')
        output.table(found['orphaned_media'], columns=['barcode', 'path'])
        print('\nPut one back with: mhvtl tape adopt <library> <barcode>')

    if not any(found.get(key) for key in
               ('orphaned_drives', 'orphaned_files', 'orphaned_services',
                'orphaned_media')):
        print('Nothing is orphaned.')
    return output.EXIT_OK


# -- changing --------------------------------------------------------------

def do_create(args) -> int:
    """The same sequence the web form runs: validate, write, start, verify,
    make the tape files (services/libraries/workflow). It used to stop after
    writing, so a library created here had barcodes and no tapes."""
    spec = {'profile': args.profile}
    for key, value in (('library_id', args.library_id),
                       ('num_drives', args.num_drives),
                       ('media_type', args.media_type),
                       ('media_count', args.media_count),
                       ('empty_slots', args.empty_slots),
                       ('library_model', args.library_model),
                       ('product', args.library_model),
                       ('drive_model', args.drive_model),
                       ('drive_product', args.drive_model),
                       ('serial', args.serial)):
        if value is not None:
            spec[key] = value

    if args.dry_run:
        return _preview(args, spec)

    privileges.require_write_access('creating a library')
    from apps.libraries.services.libraries import create_library_workflow

    if 'library_id' not in spec:
        allocated = _service(args).next_id()
        if not allocated.success:
            return output.result(allocated, as_json=args.json, quiet=args.quiet)
        spec['library_id'] = allocated.data['library_id']

    result = create_library_workflow(spec, restart=not args.no_start,
                                     create_media=not args.no_media,
                                     config_directory=args.config_dir)
    if not args.json and not args.quiet:
        for step in (result.data or {}).get('steps', []):
            mark = 'ok  ' if step['ok'] else ('FAIL' if step['fatal'] else 'warn')
            print(f'{mark} {step["step"]:9s} {step["message"]}')
    return output.result(result, as_json=args.json, quiet=args.quiet)


def _preview(args, spec) -> int:
    from apps.libraries.services.libraries import lifecycle

    if 'library_id' not in spec:
        allocated = _service(args).next_id()
        if not allocated.success:
            return output.result(allocated, as_json=args.json)
        spec['library_id'] = allocated.data['library_id']
    result = lifecycle.preview(spec, args.config_dir)
    if not result.success:
        return output.result(result, as_json=args.json)

    # An error is what create() would refuse, a warning is what it would
    # accept and grumble about: they are printed apart and only an error is a
    # failure. The preview used to call both "warning" and exit 1 for either,
    # so a perfectly creatable library - LTO3 in a drive that only reads it -
    # looked like a refusal.
    errors, warnings = result.data['errors'], result.data['warnings']
    code = output.EXIT_FAILED if errors else output.EXIT_OK
    if args.json:
        output.result(result, as_json=True)
        return code

    # The device.conf text is the data, on stdout; the problems are
    # diagnostics, on stderr, so a redirected preview stays a config file.
    print(result.data['text'], end='')
    for error in errors:
        output.note(f'error: {error}')
    for warning in warnings:
        output.note(f'warning: {warning}')
    return code


def do_slots(args) -> int:
    """Read the slot counts, or change how many are empty.

    The daemon reads library_contents once at start, so a change only reaches
    the robot after a restart; --restart does it, and without it the command
    says so rather than leaving the operator with a file the library ignores.
    """
    from apps.libraries.services.config.service import ConfigService
    from apps.libraries.services.libraries import lifecycle

    if args.empty is None and args.add is None:
        contents = ConfigService(args.config_dir).library_contents(args.library_id)
        if contents is None:
            return output.fail(f'cannot read library_contents.{args.library_id}',
                               'is the library configured, and readable?')
        summary = contents.summary()
        if args.json:
            output.emit_json({'library_id': args.library_id, **summary})
            return output.EXIT_OK
        output.pairs(summary)
        return output.EXIT_OK

    privileges.require_write_access('changing a library\'s slots')

    wanted = args.empty
    if wanted is None:
        contents = ConfigService(args.config_dir).library_contents(args.library_id)
        if contents is None:
            return output.fail(f'cannot read library_contents.{args.library_id}',
                               'is the library configured, and readable?')
        wanted = contents.summary()['empty_slots'] + args.add

    result = lifecycle.set_empty_slots(args.library_id, wanted, args.config_dir)
    if not result.success or args.json:
        return output.result(result, as_json=args.json, quiet=args.quiet)

    if not args.quiet:
        print(result.message)
    if args.restart:
        from apps.libraries.services.console import units
        restarted = units.restart_library(args.library_id)
        if not restarted.get('ok'):
            return output.fail(
                f'The slots were written but library {args.library_id} was not restarted',
                'restart it with: mhvtl service restart --library '
                f'{args.library_id}')
        if not args.quiet:
            print(f'Restarted vtllibrary@{args.library_id}.service')
    elif not args.quiet:
        print(f'Restart the library for its robot to see this: '
              f'mhvtl service restart --library {args.library_id}')
    return output.EXIT_OK


def do_delete(args) -> int:
    privileges.require_write_access('deleting a library')
    result = _service(args).delete(args.library_id, force=args.force,
                                   remove_media=args.remove_media)
    return output.result(result, as_json=args.json, quiet=args.quiet)
