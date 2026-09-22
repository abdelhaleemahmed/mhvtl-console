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
    backups = (Path(service.config_dir) / 'backups').resolve()
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
    from apps.libraries.services.sync.service import sync_mhvtl_to_django

    summary = sync_mhvtl_to_django()
    if args.json:
        output.emit_json(summary)
    elif not args.quiet:
        output.pairs(summary)
    return output.EXIT_OK
