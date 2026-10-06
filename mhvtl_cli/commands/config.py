"""list, show, validate, export, backup, restore, regenerate, sync.

The configuration files themselves. Every verb is one call into
services/config, which applies the allowlist - only device.conf, mhvtl.conf and
library_contents.N - and reads through sudo when a file is not readable
directly.

`sync` is the one verb that writes to the web UI's database rather than to the
configuration. The files are authoritative and the database is a cache of them,
so sync only ever goes that way: files into the database.
"""
import sys
from pathlib import Path

from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser('config', help='the MHVTL configuration files')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='the configuration files present')
    listing.set_defaults(handler=do_list)

    show = verbs.add_parser('show', help='print one configuration file')
    show.add_argument('name', help='device.conf, mhvtl.conf or library_contents.N')
    show.set_defaults(handler=do_show)

    validate = verbs.add_parser('validate',
                                help='check the files are consistent with each other')
    validate.set_defaults(handler=do_validate)

    export = verbs.add_parser('export', help='write every file into a zip')
    export.add_argument('output', help='path of the zip to write, or - for stdout')
    export.set_defaults(handler=do_export)

    backup = verbs.add_parser('backup', help='copy the files to a dated backup')
    backup.set_defaults(handler=do_backup)

    backups = verbs.add_parser('backups', help='every backup that has been taken')
    backups.add_argument('--files', action='store_true',
                         help='list what each backup holds')
    backups.set_defaults(handler=do_backups)

    remove_backup = verbs.add_parser('remove-backup', help='delete one backup')
    remove_backup.add_argument('name', help='as `config backups` lists it')
    remove_backup.set_defaults(handler=do_remove_backup)

    prune = verbs.add_parser(
        'prune-backups', help='delete all but the newest N backups')
    prune.add_argument('--keep', type=int, required=True, metavar='N',
                       help='how many of the newest to keep; at least 1. '
                            'Required, so a forgotten argument cannot delete '
                            'everything')
    prune.add_argument('--dry-run', action='store_true',
                       help='say what would go, change nothing')
    prune.set_defaults(handler=do_prune_backups)

    restore = verbs.add_parser('restore', help='copy a backup back into place')
    restore.add_argument('backup_dir',
                         help='a directory under the configuration backups/')
    restore.set_defaults(handler=do_restore)

    regenerate = verbs.add_parser(
        'regenerate', help='rewrite library_contents from device.conf')
    regenerate.add_argument('--force', action='store_true',
                            help='overwrite existing library_contents files')
    regenerate.set_defaults(handler=do_regenerate)

    sync = verbs.add_parser('sync', help="update the web UI's database from the files")
    sync.set_defaults(handler=do_sync)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.config.service import ConfigService
    return ConfigService(args.config_dir)


def do_list(args) -> int:
    service = _service(args)
    files = [entry.to_dict() for entry in service.files()]
    if args.json:
        output.emit_json({'config_dir': str(service.config_dir), 'files': files})
        return output.EXIT_OK

    from datetime import datetime

    print(f'{service.config_dir}:')
    rows = [{**entry, 'modified': datetime.fromtimestamp(entry['modified'])
             .strftime('%Y-%m-%d %H:%M') if entry.get('modified') else None}
            for entry in files]
    output.table(rows, columns=['name', 'size', 'modified', 'readable'])
    return output.EXIT_OK


def do_show(args) -> int:
    from apps.libraries.services.config import inventory

    if not inventory.is_allowed(args.name):
        return output.fail(f'{args.name} is not a configuration file',
                           'allowed: device.conf, mhvtl.conf, library_contents.N',
                           code=output.EXIT_USAGE)

    text = _service(args).read_file(args.name)
    if text is None:
        return output.fail(f'{args.name} does not exist or cannot be read')
    if args.json:
        output.emit_json({'name': args.name, 'content': text})
    else:
        sys.stdout.write(text)
    return output.EXIT_OK


def do_validate(args) -> int:
    result = _service(args).validate()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    print(result.message)
    return output.EXIT_OK


def do_export(args) -> int:
    data = _service(args).export_zip()
    if args.output == '-':
        sys.stdout.buffer.write(data)
        return output.EXIT_OK

    target = Path(args.output)
    try:
        target.write_bytes(data)
    except OSError as exc:
        return output.fail(f'could not write {target}: {exc}')
    if not args.quiet:
        output.note(f'wrote {len(data)} bytes to {target}')
    return output.EXIT_OK


def do_backup(args) -> int:
    privileges.require_write_access('backing up the configuration')
    return output.result(_service(args).backup(), as_json=args.json,
                         quiet=args.quiet)


def do_restore(args) -> int:
    """Restore only from a directory inside this configuration's backups/.

    restore() copies every file it finds over the live configuration, so the
    source is held to the place backup() writes to rather than taken from
    anywhere on the filesystem.
    """
    privileges.require_write_access('restoring the configuration')
    service = _service(args)
    backups = service.backup_dir.resolve()
    source = Path(args.backup_dir)
    if not source.is_absolute():
        source = backups / source
    source = source.resolve()

    if backups not in source.parents:
        return output.fail(f'{source} is not a backup of this configuration',
                           f'backups are under {backups}',
                           code=output.EXIT_USAGE)
    return output.result(service.restore(source), as_json=args.json,
                         quiet=args.quiet)


def do_regenerate(args) -> int:
    privileges.require_write_access('regenerating library_contents')
    return output.result(_service(args).regenerate_library_contents(force=args.force),
                         as_json=args.json, quiet=args.quiet)


def do_sync(args) -> int:
    """device.conf into the database, and what it changed.

    One call, rendered like every other service result. It used to print the
    counts with no message and catch nothing, so an unreadable device.conf -
    an ordinary condition, and one the web reports in a sentence - came out
    as a Python traceback under "this is a bug".
    """
    from apps.libraries.services.sync.service import sync_mhvtl_to_django

    result = sync_mhvtl_to_django(args.config_dir)
    if not args.json and result.success and not args.quiet:
        output.pairs(result.data)
    return output.result(result, as_json=args.json, quiet=args.quiet)


def do_backups(args) -> int:
    """What backups exist, newest first.

    Every config write takes one and nothing used to look at them again, so
    `config restore` took a directory name the operator had to already know.
    """
    result = _service(args).backups()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    rows = result.data['backups']
    if not rows:
        print(f"No backups in {result.data['path']}.")
        return output.EXIT_OK

    for row in rows:
        row['kib'] = row['bytes'] // 1024
        row['when'] = row['taken'] or '-'
        row['ours'] = 'yes' if row['recognised'] else 'no'
        row['holds'] = ', '.join(row['files']) if args.files else row['file_count']

    output.table(rows,
                 columns=['name', 'when', 'kib', 'holds', 'ours'],
                 headers=['backup', 'taken', 'KiB',
                          'files' if args.files else 'files', 'ours'])
    total = result.data['total_bytes']
    print(f"\n{len(rows)} backup(s), {total // 1024} KiB in "
          f"{result.data['path']}")
    unrecognised = [r for r in rows if not r['recognised']]
    if unrecognised:
        # Listed but never pruned: somebody's deliberate copy from before the
        # dated-directory shape, and a prune is not the place to decide about it.
        print(f'{len(unrecognised)} of these predate the dated-directory shape; '
              f'prune-backups leaves them alone.')
    return output.EXIT_OK


def do_remove_backup(args) -> int:
    privileges.require_write_access('removing a backup')
    return output.result(_service(args).remove_backup(args.name),
                         as_json=args.json, quiet=args.quiet)


def do_prune_backups(args) -> int:
    """Delete all but the newest --keep backups.

    --keep is required rather than defaulted: a prune that deletes everything
    because an argument was forgotten is not a prune.
    """
    if not args.dry_run:
        privileges.require_write_access('removing backups')
    result = _service(args).prune_backups(keep=args.keep, dry_run=args.dry_run)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    print(result.message)
    if args.dry_run:
        for name in (result.data or {}).get('would_remove', []):
            print(f'  {name}')
    return output.EXIT_OK
