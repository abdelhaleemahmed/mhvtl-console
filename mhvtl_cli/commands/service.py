"""start, stop, restart, status.

The systemd side: mhvtl.target and the per-library and per-drive instances.

Stopping the target stops every library and every drive on the host. That is
what an operator asking to stop MHVTL means, but it is worth the confirmation
saying so, because "stop" reads as narrower than it is.
"""
from .. import output, privileges

UNIT_ACTIONS = ('start', 'stop', 'restart')


def register(subparsers) -> None:
    parser = subparsers.add_parser('service', help='the MHVTL systemd units')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    for action in UNIT_ACTIONS:
        verb = verbs.add_parser(action, help=f'{action} MHVTL, or one library')
        verb.add_argument('--library', type=int, dest='library_id',
                          help='only this library, instead of the whole target')
        verb.set_defaults(handler=_make_handler(action), action=action)

    status = verbs.add_parser('status', help='what is running')
    status.set_defaults(handler=do_status)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _make_handler(action):
    def handler(args) -> int:
        from apps.libraries.services.console import units

        target = (f'library {args.library_id}' if args.library_id
                  else 'MHVTL on this host')
        privileges.require_write_access(f'{action}ing {target}')

        if args.library_id:
            from apps.libraries.services.config.service import ConfigService

            conf = ConfigService(args.config_dir).device_conf()
            if conf is None or args.library_id not in conf.libraries:
                return output.fail(f'library {args.library_id} is not in device.conf')
            drive_ids = sorted(conf.drives_of(args.library_id))

            if action == 'stop':
                outcome = units.stop_library(args.library_id, drive_ids)
            elif action == 'start':
                outcome = units.start_library(args.library_id, drive_ids)
            else:
                outcome = units.restart_library(args.library_id)
                return _report_restart(args, outcome)

            failed = [unit for unit, ok in outcome.items() if not ok]
            if args.json:
                output.emit_json({'action': action, 'library_id': args.library_id,
                                  'units': outcome, 'failed': failed})
                return output.EXIT_OK if not failed else output.EXIT_FAILED
            if failed:
                return output.fail(f'{len(failed)} unit(s) did not {action}',
                                   *failed)
            if not args.quiet:
                print(f'Library {args.library_id}: {len(outcome)} unit(s) '
                      f'{action}ed')
            return output.EXIT_OK

        # The whole target: every library and every drive on the host.
        result = getattr(units, action)()
        if args.json:
            output.emit_json({'action': action, 'unit': units.TARGET,
                              'ok': result.ok,
                              'output': result.output.strip()})
            return output.EXIT_OK if result.ok else output.EXIT_FAILED
        if not result.ok:
            return output.fail(f'could not {action} {units.TARGET}',
                               result.output.strip()[:300])
        if not args.quiet:
            print(f'{units.TARGET} {action}ed - every library and drive on '
                  f'this host')
        return output.EXIT_OK

    return handler


def _report_restart(args, outcome) -> int:
    if args.json:
        output.emit_json(outcome)
        return output.EXIT_OK if outcome['ok'] else output.EXIT_FAILED
    if not outcome['ok']:
        return output.fail(f'could not restart {outcome["restarted"]}',
                           outcome.get('error') or '')
    if not args.quiet:
        print(f'Restarted {outcome["restarted"]}')
    return output.EXIT_OK


def do_status(args) -> int:
    """The same answer as `mhvtl status system`, under the verb an operator
    reaches for after start or stop."""
    from .status import do_system
    return do_system(args)
