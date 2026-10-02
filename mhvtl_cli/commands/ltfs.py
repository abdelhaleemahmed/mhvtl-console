"""tapes, drives, mount, unmount, check, format, support.

Its own noun, because "mount" already means something else here: `mhvtl op
mount` is the robot putting a cartridge into a drive, and this is a filesystem
over a cartridge already in one. Two different operations at two different
levels, and burying the second under `tape` while the first lived under `op`
made that harder to see rather than easier.

Argument handling and printing only. Every verb is one call into
services/ltfs or services/tapes, and the web console's Operator > LTFS page
calls the same methods - so neither front end decides anything.

`format` is the only verb here that destroys data, and it is refused unless
MHVTL_GUI_ALLOW_LTFS_FORMAT is set. The refusal comes from the service, in
words, rather than from sudo.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        'ltfs', help='cartridges as filesystems: state, mount, check')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    tapes = verbs.add_parser('tapes', help='which cartridges are LTFS volumes')
    tapes.add_argument('library_id', type=int)
    tapes.set_defaults(handler=do_tapes)

    drives = verbs.add_parser(
        'drives', help="each drive's LTFS state, and what is mounted")
    drives.add_argument('library_id', type=int)
    drives.set_defaults(handler=do_drives)

    mount = verbs.add_parser(
        'mount', help='mount the cartridge in a drive as a filesystem')
    mount.add_argument('library_id', type=int)
    mount.add_argument('drive', type=int, help='mtx drive number, from 0')
    mount.set_defaults(handler=do_mount)

    unmount = verbs.add_parser(
        'unmount', help='release the volume and write its index to the tape')
    unmount.add_argument('library_id', type=int)
    unmount.add_argument('drive', type=int, help='mtx drive number, from 0')
    unmount.set_defaults(handler=do_unmount)

    check = verbs.add_parser('check', help="ltfsck a cartridge's consistency")
    check.add_argument('library_id', type=int)
    check.add_argument('drive', type=int, help='mtx drive number, from 0')
    check.set_defaults(handler=do_check)

    fmt = verbs.add_parser(
        'format', help='mkltfs a cartridge - ERASES IT - off unless enabled')
    fmt.add_argument('library_id', type=int)
    fmt.add_argument('drive', type=int, help='mtx drive number, from 0')
    fmt.set_defaults(handler=do_format)

    support = verbs.add_parser(
        'support', help='which profiles and drive models LTFS can open')
    support.set_defaults(handler=do_support)

    provisioning = verbs.add_parser(
        'provisioning',
        help='what a library has for LTFS, what it lacks, and what it can be '
             'given')
    provisioning.add_argument('library_id', type=int)
    provisioning.set_defaults(handler=do_provisioning)

    add_media = verbs.add_parser(
        'add-media',
        help='create cartridges the library\'s LTFS drive can format')
    add_media.add_argument('library_id', type=int)
    add_media.add_argument('--count', type=int, required=True, metavar='N',
                           help='how many cartridges. Required, so a forgotten '
                                'argument cannot create a library full of them')
    add_media.add_argument('--density', metavar='LTO8',
                           help="the newest density the library's LTFS drive "
                                'writes, if omitted')
    add_media.add_argument('--expand-slots', action='store_true',
                           help='grow the library if it has too few empty '
                                'slots (off by default: the slot count is part '
                                'of what the library advertises)')
    add_media.set_defaults(handler=do_add_media)

    add_drive = verbs.add_parser(
        'add-drive',
        help='add a drive LTFS can open to a library that has none')
    add_drive.add_argument('library_id', type=int)
    add_drive.add_argument('--vendor',
                           help='vendor id the drive reports; one LTFS knows is '
                                'chosen if omitted. Never substituted: a vendor '
                                'LTFS does not know is refused, not replaced')
    add_drive.add_argument('--model', help="drive model; the library's own if "
                                           'LTFS opens it')
    add_drive.add_argument('--drive-revision', dest='revision', metavar='REV',
                           help='firmware revision. Only IBM LTO-5, LTO-8 and '
                                'TS1140 have a minimum at all')
    add_drive.add_argument('--tapes', type=int, default=0, metavar='N',
                           help='also create N cartridges (default: none)')
    add_drive.add_argument('--expand-slots', action='store_true',
                           help='grow the library if it has too few empty slots '
                                '(off by default: the slot count is part of what '
                                'the library advertises)')
    add_drive.set_defaults(handler=do_add_drive)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.ltfs import LtfsService
    return LtfsService(args.config_dir)


def _tape_service(args):
    from apps.libraries.services.tapes import TapeService
    return TapeService(args.config_dir)


def do_tapes(args) -> int:
    """Which cartridges LTFS formatted, and which only look as though it did."""
    result = _tape_service(args).list(args.library_id, with_usage=True,
                                      with_ltfs=True)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    tapes = result.data['tapes']
    if not tapes:
        print(f'Library {args.library_id} holds no tapes.')
        return output.EXIT_OK

    # Every cartridge unread means a gate answered the library instead: either
    # no drive here opens LTFS, or the media is too old to be partitioned. Say
    # which, rather than printing a table of blanks - the state column would
    # otherwise read as "we looked and could not tell".
    if all(not t.get('ltfs_state') for t in tapes):
        print(f'No cartridge in library {args.library_id} was read, because no '
              f'drive in it can open LTFS.')
        print('The cartridges may still be LTFS volumes - written elsewhere and '
              'carried here - so this is not an answer about them.')
        print(f"\nAdd a drive LTFS can use:  mhvtl ltfs add-drive "
              f"{args.library_id}")
        return output.EXIT_OK

    output.table(tapes,
                 columns=['barcode', 'partitions', 'layout', 'ltfs_state',
                          'ltfs_summary'],
                 headers=['barcode', 'parts', 'layout', 'state',
                          'what that means'])

    volumes = [t for t in tapes if t.get('ltfs')]
    former = [t for t in tapes if t.get('ltfs_was')]
    unknown = [t for t in tapes if t.get('ltfs_state') == 'unknown']
    print(f'\n{len(volumes)} of {len(tapes)} cartridge(s) are LTFS volumes.')
    if former:
        # Worth saying out loud: the MAM still claims LTFS on these, so an
        # operator who reads the cartridge directly will disagree with us.
        print(f'{len(former)} carr{"ies" if len(former) == 1 else "y"} LTFS '
              'attributes but no second partition - formatted once, since '
              'unpartitioned, and not mountable.')
    if unknown:
        print(f'{len(unknown)} could not be read (1.7 media, or busy).')
    return output.EXIT_OK


def do_drives(args) -> int:
    """Each drive: what LTFS makes of it, what is loaded, what is mounted."""
    result = _service(args).status(args.library_id)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    output.table(result.data['drives'],
                 columns=['drive_num', 'drive_id', 'model', 'device',
                          'ltfs_capable', 'barcode', 'cartridge_state',
                          'mounted_at'],
                 headers=['drive', 'id', 'model', 'node', 'LTFS can open',
                          'cartridge', 'state', 'mounted at'])
    for row in result.data['drives']:
        if not row['ltfs_capable'] and row['ltfs_reason']:
            print(f"  drive {row['drive_num']}: {row['ltfs_reason']}")
    missing = [name for name, path in result.data['tools'].items() if not path]
    if missing:
        print(f"\nNot installed: {', '.join(missing)}")
    if not result.data['format_allowed']:
        print('Formatting is disabled on this host (MHVTL_GUI_ALLOW_LTFS_FORMAT).')
    return output.EXIT_OK


def do_mount(args) -> int:
    privileges.require_write_access('mounting an LTFS volume')
    result = _service(args).mount(args.library_id, args.drive)
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_unmount(args) -> int:
    privileges.require_write_access('unmounting an LTFS volume')
    result = _service(args).unmount(args.library_id, args.drive)
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_check(args) -> int:
    """ltfsck. Its exit 1 means 'consistent, cartridge modified', so the service
    reports success and the code is in the data rather than returned - a caller
    treating non-zero as failure would call every healthy volume broken."""
    result = _service(args).check(args.library_id, args.drive)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    print(result.message)
    if result.data.get('corrected'):
        print('  ltfsck rewrote the MAM coherency data, so the cartridge was '
              'modified (exit 1 = LTFSCK_CORRECTED).')
    return output.EXIT_OK


def do_format(args) -> int:
    """mkltfs. Destroys everything on the cartridge."""
    privileges.require_write_access('formatting a cartridge')
    result = _service(args).format_cartridge(args.library_id, args.drive)
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_support(args) -> int:
    """Which profiles and drive models LTFS can open, before a library is built.

    Said before creation because the drive's vendor id, product id and firmware
    revision are decided then and are awkward to change afterwards. The facts
    come from services/profiles/ltfs_support.py, which the suite checks against
    the LTFS source.
    """
    from apps.libraries.services.profiles import PROFILES, ltfs_support

    rows = []
    for key, profile in sorted(PROFILES.items()):
        usable = ltfs_support.usable_drive_models(profile.drive_vendor)
        offered = [m for m in profile.drive_models if m in usable]
        gated = sorted({m for m in offered
                        if ltfs_support.firmware_minimum_for(
                            usable[m], profile.drive_vendor)})
        verdict = ltfs_support.supports(profile.drive_vendor,
                                        profile.drive_product_default,
                                        profile.drive_revision_default)
        rows.append({
            'profile': key,
            'vendor': profile.drive_vendor,
            'known': 'yes' if usable else 'NO',
            'usable': f'{len(offered)} of {len(profile.drive_models)}',
            'default_ok': 'yes' if verdict.supported else 'no',
            'why': verdict.reason or '',
            'gated': ', '.join(gated) or '-',
        })

    if args.json:
        output.emit_json({'profiles': rows})
        return output.EXIT_OK

    output.table(rows,
                 columns=['profile', 'vendor', 'known', 'usable', 'default_ok'],
                 headers=['profile', 'drive vendor', 'LTFS knows it',
                          'models it can open', 'default usable'])
    print()
    for row in rows:
        if row['why']:
            print(f"  {row['profile']:<9} {row['why']}")
    print("\nWhere 'default usable' is no but 'LTFS knows it' is yes, the model "
          "is fine and\nthe firmware revision is not: pass "
          "`library create --drive-revision`. Gated models:")
    for row in rows:
        if row['gated'] != '-':
            print(f"  {row['profile']:<9} {row['gated']}")
    return output.EXIT_OK


def do_add_drive(args) -> int:
    """Make an existing library LTFS-capable by adding one drive to it.

    The same workflow the console's LTFS page posts to. It adds and never
    removes: if there is no room it says what is full, because taking a drive
    away restarts a library and is an operator's decision of its own.

    It stops at "a drive LTFS can open, and cartridges in slots" - it does not
    format anything. mkltfs erases a cartridge and stays a separate act.
    """
    from .. import privileges
    from apps.libraries.services.libraries import add_ltfs_drive_workflow

    privileges.require_write_access('adding a drive to a library')

    result = add_ltfs_drive_workflow(
        args.library_id, vendor=args.vendor, model=args.model,
        revision=args.revision, tapes=args.tapes,
        expand_slots=args.expand_slots, config_directory=args.config_dir)

    if not args.json and not args.quiet:
        for step in (result.data or {}).get('steps', []):
            mark = 'ok  ' if step['ok'] else ('FAIL' if step['fatal'] else 'warn')
            print(f'{mark} {step["step"]:9s} {step["message"]}')
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_provisioning(args) -> int:
    """What this library has for LTFS, what it lacks, and what it can be given.

    Reading only. The same call the console's LTFS page makes, so the two
    cannot disagree about which drive is capable or how many slots are free.
    """
    from apps.libraries.services.libraries import ltfs_provisioning

    result = ltfs_provisioning(args.library_id, args.config_dir)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    data = result.data
    print(result.message)
    print()

    drives = [{
        'drive': d['drive_id'],
        'model': f"{d['vendor']} {d['product']}".strip(),
        'rev': d['revision'] or '-',
        'ltfs': 'yes' if d['ltfs_capable'] else 'no',
        'why': d['ltfs_reason'] or '',
    } for d in data['drives']]
    if drives:
        output.table(drives, columns=['drive', 'model', 'rev', 'ltfs', 'why'],
                     headers=['drive', 'model', 'firmware', 'LTFS can open',
                              'why not'])
        print()

    slots = data['drive_slots']
    storage = data['storage_slots']
    print(f"Drive slots    {slots['used']} used"
          + (f" of {slots['max']}" if slots['max'] else ''))
    print(f"Storage slots  {storage['full']} full, {storage['empty']} empty, "
          f"{storage['total']} total")
    # None rather than 0: with no capable drive the cartridges were not read,
    # and "we did not look" is not "there are none".
    if data['ltfs_media'] is None:
        print('LTFS volumes   not read - no drive here can open LTFS')
    else:
        print(f"LTFS volumes   {data['ltfs_media']}"
              + (f"  {', '.join(data['ltfs_media_barcodes'][:6])}"
                 if data['ltfs_media_barcodes'] else ''))

    if not data['needs']:
        print('\nThis library is ready for LTFS.')
        return output.EXIT_OK

    print()
    if 'drive' in data['needs']:
        if data['can_add_drive']:
            candidates = [{
                'vendor': c['vendor'], 'model': c['model'],
                'firmware': c['firmware_minimum'] or '-',
            } for c in data['drive_candidates']]
            print('It needs a drive LTFS can open. It can be given:')
            output.table(candidates, columns=['vendor', 'model', 'firmware'],
                         headers=['vendor', 'model', 'firmware at least'])
            print(f"\n    mhvtl ltfs add-drive {args.library_id}")
        else:
            print(f"It needs a drive LTFS can open, and one cannot be added: "
                  f"{data['cannot_add_because']}")
    if 'media' in data['needs']:
        usable = [m['density'] for m in data['media_candidates'] if m['usable']]
        if data['can_add_media']:
            print(f"It needs cartridges. Densities its drive can format: "
                  f"{', '.join(usable)}")
            print(f"\n    mhvtl ltfs add-media {args.library_id} --count 2")
        else:
            print(f"It needs cartridges, and none can be added: "
                  f"{data['cannot_add_media_because']}")
    return output.EXIT_OK


def do_add_media(args) -> int:
    """Create cartridges the library's LTFS drive can format.

    Blank media of a density LTFS can work with - it does not format anything.
    A cartridge becomes an LTFS volume through mkltfs, which erases it.
    """
    from .. import privileges
    from apps.libraries.services.libraries import add_ltfs_media_workflow

    privileges.require_write_access('creating cartridges')

    result = add_ltfs_media_workflow(
        args.library_id, args.count, density=args.density,
        expand_slots=args.expand_slots, config_directory=args.config_dir)

    if not args.json and not args.quiet:
        for step in (result.data or {}).get('steps', []):
            mark = 'ok  ' if step['ok'] else ('FAIL' if step['fatal'] else 'warn')
            print(f'{mark} {step["step"]:9s} {step["message"]}')
    return output.result(result, as_json=args.json, quiet=args.quiet)
