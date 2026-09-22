"""logs, modules, disk, system.

The host, rather than the library: what the console pages show.

`logs` reads an allowlisted file through services/console/logs, which is the
whole point of it being there - the path used to be interpolated into a
shell=True command, so a log name was a way to run anything.
"""
from .. import output


def register(subparsers) -> None:
    parser = subparsers.add_parser('console', help='host state and logs')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    logs = verbs.add_parser('logs', help='tail an allowlisted log')
    logs.add_argument('path', nargs='?', default='/var/log/messages')
    logs.add_argument('-n', '--lines', type=int, default=50)
    logs.set_defaults(handler=do_logs)

    modules = verbs.add_parser('modules', help='kernel module state')
    modules.set_defaults(handler=do_modules)

    disk = verbs.add_parser('disk', help='space on the config and media directories')
    disk.set_defaults(handler=do_disk)

    system = verbs.add_parser('system', help='kernel, uptime, load, memory')
    system.set_defaults(handler=do_system)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def do_logs(args) -> int:
    from apps.libraries.services.console import logs

    answer = logs.read(args.path, lines=args.lines)
    if args.json:
        output.emit_json(answer)
        return output.EXIT_OK if answer['success'] else output.EXIT_FAILED
    if not answer['success']:
        return output.fail(answer.get('error', 'could not read the log'),
                           f'allowlisted paths: '
                           f'{", ".join(getattr(logs, "ALLOWED", []) or [])}')
    for line in answer['lines']:
        print(line)
    return output.EXIT_OK


def do_modules(args) -> int:
    from apps.libraries.services.console import modules

    summary = modules.summary()
    if args.json:
        output.emit_json(summary)
        return output.EXIT_OK

    print(f'backend: {summary["backend"]}')
    rows = [{'module': name, **info} for name, info in summary['modules'].items()]
    output.table(rows, columns=['module', 'loaded', 'required'])
    return output.EXIT_OK


def do_disk(args) -> int:
    from apps.libraries.services.console import disk

    usages = disk.usage()
    if args.json:
        output.emit_json([entry.to_dict() for entry in usages])
        return output.EXIT_OK

    output.table([entry.to_dict() for entry in usages],
                 columns=['path', 'size', 'used', 'available', 'percent'],
                 headers=['path', 'size', 'used', 'free', 'used %'])
    return output.EXIT_OK


def do_system(args) -> int:
    from apps.libraries.services.console import system

    info = system.info()
    data = info.to_dict() if hasattr(info, 'to_dict') else vars(info)
    if args.json:
        output.emit_json(data)
        return output.EXIT_OK
    output.pairs(data)
    return output.EXIT_OK
