"""list, media, create, bulk, delete, next-barcode, slots.

Argument handling and printing only; every verb is one call into services/tapes,
which validates the barcode before it reaches `sudo mktape` or `sudo rm -rf`.

`delete --remove-media` is the only verb here that destroys data. It is spelled
out rather than defaulted, and the confirmation says how many tapes went, because
"the tape is no longer in the library" and "the tape's data is gone" are
different outcomes and an operator should not discover which they chose
afterwards.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser('tape', help='cartridges and their media files')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='every tape in a library')
    listing.add_argument('library_id', type=int)
    listing.add_argument('--no-usage', action='store_true',
                         help='skip measuring how full each tape is')
    listing.set_defaults(handler=do_list)

    media = verbs.add_parser('media',
                             help="which tapes the library's drives take")
    media.add_argument('library_id', type=int)
    media.set_defaults(handler=do_media)

    slots = verbs.add_parser('slots', help='how many slots are used and free')
    slots.add_argument('library_id', type=int)
    slots.set_defaults(handler=do_slots)

    next_barcode = verbs.add_parser('next-barcode',
                                    help='the next free barcode in the series')
    next_barcode.add_argument('library_id', type=int)
    next_barcode.add_argument('--prefix', help='override the detected prefix')
    next_barcode.add_argument('--suffix', help='override the detected suffix')
    next_barcode.set_defaults(handler=do_next_barcode)

    create = verbs.add_parser('create', help='create one tape')
    create.add_argument('library_id', type=int)
    create.add_argument('barcode')
    create.add_argument('--slot', type=int, help='slot to put it in')
    create.add_argument('--size-mb', type=int, default=500000,
                        help='capacity in MB (default 500000)')
    create.add_argument('--density',
                        help='e.g. LTO8; read from the barcode if omitted. '
                             "Must be one the library's drives load "
                             "(see 'tape media')")
    create.add_argument('--kind', choices=('data', 'clean', 'WORM'),
                        help='read from the barcode if omitted')
    create.set_defaults(handler=do_create)

    bulk = verbs.add_parser('bulk', help='create a run of tapes')
    bulk.add_argument('library_id', type=int)
    bulk.add_argument('count', type=int)
    bulk.add_argument('--prefix', help='override the detected prefix')
    bulk.add_argument('--suffix', help='override the detected suffix')
    bulk.add_argument('--start', type=int, dest='start_number',
                      help='first number; the next free one if omitted')
    bulk.add_argument('--size-mb', type=int, default=500000)
    bulk.add_argument('--density',
                      help="e.g. LTO8; picks the suffix when --suffix is not "
                           "given. Must be one the library's drives load")
    bulk.add_argument('--kind', choices=('data', 'clean', 'WORM'),
                      help='WORM runs get the WORM suffix for LTO densities')
    bulk.set_defaults(handler=do_bulk)

    adopt = verbs.add_parser(
        'adopt', help='put a tape that still has its files into a library')
    adopt.add_argument('library_id', type=int)
    adopt.add_argument('barcode')
    adopt.add_argument('--slot', type=int, help='slot to put it in')
    adopt.set_defaults(handler=do_adopt)

    delete = verbs.add_parser('delete', help='remove a tape from its slot')
    delete.add_argument('library_id', type=int)
    delete.add_argument('barcode')
    delete.add_argument('--remove-media', action='store_true',
                        help='also delete the data, which cannot be undone')
    delete.set_defaults(handler=do_delete)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.tapes import TapeService
    return TapeService(args.config_dir)


# -- reading ---------------------------------------------------------------

def do_list(args) -> int:
    result = _service(args).list(args.library_id, with_usage=not args.no_usage)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    tapes = result.data['tapes']
    if not tapes:
        print(f'Library {args.library_id} holds no tapes.')
        return output.EXIT_OK

    columns = ['barcode', 'slot', 'kind', 'density']
    headers = ['barcode', 'slot', 'kind', 'density']
    if not args.no_usage:
        columns += ['used_mb', 'capacity_mb', 'media_exists']
        headers += ['used MB', 'capacity MB', 'on disk']

    output.table(tapes, columns=columns, headers=headers)
    summary = result.data['summary']
    print(f'\n{len(tapes)} tapes, {summary.get("empty_slots", "?")} empty slots')
    return output.EXIT_OK


def do_media(args) -> int:
    info = _service(args).media_for_library(args.library_id)
    if args.json:
        output.emit_json(info)
        return output.EXIT_OK
    if not info['drives']:
        return output.fail(f'library {args.library_id} has no drives in device.conf',
                           'is the library configured, and readable?')
    print(f'Drives: {", ".join(info["drives"])}')
    if not info['known']:
        print('MHVTL gives none of these drives a media list; any density may be tried.')
        return output.EXIT_OK
    rows = [{'density': m['density'], 'suffix': m['suffix'],
             'use': 'read/write' if m['writable'] else 'read-only',
             'drives': ', '.join(m['writable_in'] or m['read_only_in'])}
            for m in info['media']]
    output.table(rows, columns=['density', 'suffix', 'use', 'drives'],
                 headers=['density', 'suffix', 'use', 'in drives'])
    print(f'\nDefault for new tapes: {info["default"] or "none writable"}')
    return output.EXIT_OK


def do_slots(args) -> int:
    from apps.libraries.services.config.service import ConfigService

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


def do_next_barcode(args) -> int:
    result = _service(args).next_barcode(args.library_id, prefix=args.prefix,
                                         suffix=args.suffix)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    print(result.data['barcode'])
    return output.EXIT_OK


# -- changing --------------------------------------------------------------

def do_create(args) -> int:
    privileges.require_write_access('creating a tape')
    result = _service(args).create(args.library_id, args.barcode,
                                   slot=args.slot, size_mb=args.size_mb,
                                   density=args.density, kind=args.kind)
    code = output.result(result, as_json=args.json, quiet=args.quiet)
    if result.success and not args.json and not args.quiet:
        print(_restart_note(args.library_id))
    return code


def do_bulk(args) -> int:
    privileges.require_write_access('creating tapes')
    result = _service(args).create_bulk(args.library_id, args.count,
                                        prefix=args.prefix, suffix=args.suffix,
                                        start_number=args.start_number,
                                        size_mb=args.size_mb,
                                        density=args.density, kind=args.kind)
    code = output.result(result, as_json=args.json, quiet=args.quiet)
    if (result.data or {}).get('created') and not args.json and not args.quiet:
        print(_restart_note(args.library_id))
    return code


def do_adopt(args) -> int:
    """Give an orphaned tape back to a library, data and all.

    `library orphans` lists the tapes on disk no library claims - what a delete
    without --remove-media leaves behind. Nothing is written to the media here.
    """
    privileges.require_write_access('adopting a tape')
    result = _service(args).adopt(args.library_id, args.barcode, slot=args.slot)
    code = output.result(result, as_json=args.json, quiet=args.quiet)
    if result.success and not args.json and not args.quiet:
        print(_restart_note(args.library_id))
    return code


def _restart_note(library_id: int) -> str:
    """The robot reads library_contents once, when its daemon starts."""
    return (f'Restart the library for its robot to see this: '
            f'mhvtl service restart --library {library_id}')


def do_delete(args) -> int:
    privileges.require_write_access('deleting a tape')
    result = _service(args).delete(args.library_id, args.barcode,
                                   remove_media=args.remove_media)
    if not args.json and result.success and not args.quiet:
        if args.remove_media:
            print('The media files were deleted; this cannot be undone.')
        else:
            print(f'Its media files are kept; `mhvtl tape adopt <library> '
                  f'{result.data.get("barcode", args.barcode)}` puts it back.')
    code = output.result(result, as_json=args.json, quiet=args.quiet)
    if result.success and not args.json and not args.quiet:
        print(_restart_note(args.library_id))
    return code
