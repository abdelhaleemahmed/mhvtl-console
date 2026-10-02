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
from .. import colour, output, privileges


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

    layout = verbs.add_parser(
        'layout', help='draw the library: drives, slots and the port')
    layout.add_argument('library_id', type=int)
    layout.add_argument('--ltfs', action='store_true',
                        help='also read each cartridge and mark the LTFS '
                             'volumes (one pass over the media directory)')
    layout.add_argument('--no-colour', '--no-color', dest='no_colour',
                        action='store_true',
                        help='draw without any escape sequences')
    layout.add_argument('--width', type=int, metavar='N',
                        help='slots per row; the default fits the terminal')
    layout.set_defaults(handler=do_layout)

    palette = verbs.add_parser(
        'palette', help='the colour every tape generation is drawn in')
    palette.add_argument('--no-colour', '--no-color', dest='no_colour',
                         action='store_true',
                         help='print the table without any escape sequences')
    palette.set_defaults(handler=do_palette)

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
    """Unload a cartridge, through the guard rather than around it.

    ltfs.unmount_tape() refuses while a filesystem is mounted on the drive and
    otherwise hands the move to the same OperationsService.unmount() this used
    to call directly. The CLI gets the guard before the page does, which is
    what proves the decision is in the services: if the terminal can refuse it,
    a template is not deciding anything.
    """
    from apps.libraries.services import ltfs

    privileges.require_write_access('unmounting a tape')
    return output.result(
        ltfs.unmount_tape(args.library_id, args.drive, args.slot,
                          config_directory=args.config_dir),
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


def do_palette(args) -> int:
    """The legend for `op layout`, and the reference for the web's classes."""
    from apps.libraries.services.tapes import TapeService

    result = TapeService().palette()
    if getattr(args, 'json', False) or not result.success:
        return output.result(result, as_json=getattr(args, 'json', False))

    on = colour.enabled(getattr(args, 'no_colour', False))
    rows = []
    for entry in result.data['palette']:
        ansi = entry['ansi'] if entry['token'] != 'lto-unknown' else None
        rows.append({
            'token': entry['token'],
            'generation': entry['generation'] or output.EMPTY,
            'shell': entry['hue'],
            'ansi': entry['ansi'],
            'swatch': colour.swatch(ansi, on=on),
            'dark': entry['dark'],
            'light': entry['light'],
            'separated_by': entry['separated_by'],
        })
    # No swatch column when colour is off: a column of spaces is not a column.
    columns = ['token', 'generation', 'shell', 'ansi', 'dark', 'light',
               'separated_by']
    headers = ['token', 'generation', 'real shell', 'ansi', 'dark', 'light',
               'separated by']
    if on:
        columns.insert(4, 'swatch')
        headers.insert(4, 'swatch')
    output.table(rows, columns=columns, headers=headers)

    print()
    print('The real cartridge shell colours repeat every five generations, so '
          'these collide:')
    for group in result.data['collisions']:
        names = ', '.join(g['generation'] for g in group['generations'])
        print(f'  {group["hue"]:<14} {names}')
    print()
    print('Each keeps its real hue and is separated inside it by lightness. The '
          'generation is')
    print('always printed as text beside the colour, so nothing here is '
          'readable by colour alone.')
    if not on:
        print()
        output.note('Colour is off: stdout is not a terminal, --no-colour was '
                    'given, NO_COLOR is set, or TERM does not offer 256 '
                    'colours.')
    return output.EXIT_OK


def do_layout(args) -> int:
    """Draw a library: what is in each drive, each slot and the port.

    Everything drawn here was decided in the services - which generation a
    cartridge is, which a drive is drawn as, whether a cartridge is an LTFS
    volume. This function chooses nothing but where things sit on the screen.
    """
    from apps.libraries.services.operations import mounting
    from apps.libraries.services.tapes import palette as palette_table

    result = mounting.mount_status(args.library_id, args.config_dir,
                                   with_ltfs=getattr(args, 'ltfs', False))
    if getattr(args, 'json', False) or not result.success:
        return output.result(result, as_json=getattr(args, 'json', False))

    data = result.data
    on = colour.enabled(getattr(args, 'no_colour', False))
    ansi = {row['token']: row['ansi'] for row in palette_table.rows()}
    # Which drives hold a mounted filesystem, and so cannot be emptied. One
    # /proc/mounts read; the map says so rather than letting an operator find
    # out from a refusal. ltfs/ is a leaf and the CLI is a caller, so asking it
    # here is the allowed direction - see services/ltfs/tape_moves.py.
    from apps.libraries.services import ltfs as ltfs_package
    blocked = ltfs_package.blocked_drives(args.library_id)

    def fill(text, token):
        return colour.block(text, ansi.get(token), token, on=on)

    print(f'Library {data["library_id"]}  {data["device_path"]}')

    print()
    print('Drives')
    # Without colour the chip carries the generation in words, so the column
    # says the same thing either way rather than going blank.
    width = max((len(palette_table.label_for(d['generation_token']))
                 for d in data['drives']), default=0)
    for drive in data['drives']:
        token = drive['generation_token']
        chip = (fill('   ', token) if on
                else f'[{palette_table.label_for(token).rjust(width)}]')
        if drive['full']:
            holds = (f'{drive["barcode"] or "a tape"}'
                     f'  ({drive["tape_lto"] or "unknown generation"})')
        elif drive['lto_generation'] and drive['lto_generation'] != 'Unknown':
            holds = f'empty  takes up to {drive["lto_generation"]}'
        else:
            # A T10000 or a 3592 has no LTO generation, which is not the same
            # as one we failed to read.
            holds = 'empty  not an LTO drive'
        mark = '  LTFS' if drive.get('ltfs_capable') else ''
        if drive['drive_num'] in blocked:
            mark += '  MOUNTED'
        print(f'  {chip} Drive {drive["drive_num"]:<3} '
              f'{drive["model"]:<14} {holds}{mark}')

    print()
    counts = data['slot_summary']
    print(f'Storage slots  ({counts["full_slots"]} of '
          f'{counts["total_slots"]} full)')
    _draw_slots(data['storage_slots'], fill, on, getattr(args, 'width', None))

    ports = data['import_export_slots']
    full_ports = [p for p in ports if p['full']]
    print()
    if not ports:
        print('Import/export  none')
    elif not full_ports:
        print(f'Import/export  all {len(ports)} empty')
    else:
        print('Import/export  ' + '  '.join(
            f'{p["slot_num"]}: {p["barcode"] or "a tape"}' for p in full_ports))

    _draw_legend(data, fill, on, getattr(args, 'ltfs', False), blocked)
    if not on:
        print()
        output.note('Colour is off: stdout is not a terminal, --no-colour was '
                    'given, NO_COLOR is set, or TERM does not offer 256 '
                    'colours. Every generation is printed as text, so nothing '
                    'is lost but the fill.')
    return output.EXIT_OK


def _draw_slots(slots, fill, on, width=None) -> None:
    """The grid. A tile is the barcode written on its generation's colour.

    The barcode is never shortened to make a row fit - a barcode cut to fit a
    terminal cannot be copied, and the reader has no way to know it was cut.
    Fewer columns is the thing that gives.

    Every cell is the same width whether or not it is full and whether or not
    colour is on, so the grid reads as a grid down a column as well as across.
    """
    import shutil

    if not slots:
        print('  none')
        return

    numbers = max(len(str(s['slot_num'])) for s in slots)
    barcodes = max((len(s['barcode'] or '') for s in slots if s['full']),
                   default=6)
    inner = barcodes + 2                   # the tile, including its padding
    body_width = inner if on else inner + 2
    cell = 2 + numbers + 1 + body_width + 1   # lead, number, bars, tile
    columns = width or max(1, (shutil.get_terminal_size((100, 24)).columns - 2)
                           // cell)

    # One width for every cell, full or empty, coloured or not: a block has
    # no brackets and a bracketed barcode has no block, and a grid that drifts
    # by a character per row is not a grid.
    for start in range(0, len(slots), columns):
        line = ''
        for slot in slots[start:start + columns]:
            number = str(slot['slot_num']).rjust(numbers)
            head = f'\033[2m{number}\033[0m' if on else number
            if not slot['full']:
                dots = ('\u00b7' * barcodes).center(body_width)
                body = f'\033[2m{dots}\033[0m' if on else dots
                left, right = ' ', ' '
            else:
                text = f' {(slot["barcode"] or "").ljust(barcodes)} '
                body = fill(text, slot['generation_token']) if on else f'[{text}]'
                # The outline sits where the gap and the trailing mark used to,
                # so switching between them costs the grid nothing.
                left, right = colour.ltfs_outline(slot.get('ltfs_mark', ''),
                                                  on=on)
            line += f'  {head}{left}{body}{right}'
        print(line.rstrip())


def _draw_legend(data, fill, on, asked_for_ltfs, blocked=None) -> None:
    """Only the generations this library actually holds.

    All ten would be a wall of colour for a library with two, and the point of
    a legend is to be read.
    """
    # The service worked out which generations are here and what to call
    # them; this only decides where they sit on the line.
    present = data['generations_present']
    blocked = blocked or {}

    print()
    if on:
        entries = [f'{fill("   ", g["token"])} {g["label"]}' for g in present]
    else:
        # No fill to point at, so the legend is just what is in the library -
        # which is the fact the colour was standing in for anyway.
        entries = [g['label'] for g in present]
    print('Legend  ' + '  '.join(entries))
    if blocked:
        print('        MOUNTED - a filesystem is open on this drive, so the '
              'tape cannot be unloaded')
    if asked_for_ltfs:
        # `ltfs is None` on every slot means gate 1 answered: no drive here
        # can open LTFS, so nothing was read. Saying so beats a legend for
        # marks that cannot appear.
        if any(s.get('ltfs') is not None for s in data['storage_slots']):
            # The sample is the outline round a blank tile, so the legend
            # shows the thing the grid shows rather than describing it.
            def sample(mark):
                left, right = colour.ltfs_outline(mark, on=on)
                return f'{left}   {right}' if on else right
            print(f'        {sample("ltfs")} an LTFS volume   '
                  f'{sample("ltfs-was")} was LTFS, now unpartitioned')
        else:
            print('        No cartridge was read: no drive in this library '
                  'can open LTFS.')

