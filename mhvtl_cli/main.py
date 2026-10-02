"""Entry point and argument parser.

The CLI is a consumer of apps.libraries.services and holds no logic of its own:
a command parses arguments, calls one service method, and prints the result. If
a command starts making decisions, the decision belongs in a service where the
web UI can reach it too.

Django is set up before any service is imported, because the services read their
paths from settings - MHVTL_CONFIG_DIR and MHVTL_HOME_DIR. That is also why
--config-dir works: it overrides the setting before anything reads it.

The CLI reads /etc/mhvtl by default, whatever the Django settings say. That is
not a detail: the development settings point MHVTL_CONFIG_DIR at a
./generated_configs/ copy, and a CLI that decides what to do from a stale copy
and then acts on real systemd units and real SCSI devices is dangerous. It
happened during step 9 - `library orphans --clean` read the copy and stopped
three real units. They were genuinely orphaned in both, so the outcome was
right by luck. Every command prints which directory it used when asked, and
--config-dir overrides it deliberately.

    mhvtl library list
    mhvtl library show 10
    mhvtl status system --json
"""
import argparse
import os
import sys
from typing import List, Optional

from . import output, privileges

#: Where the Django project lives, relative to this package.
DEFAULT_SETTINGS = 'mhvtl_system.settings.development'

#: What a system administration tool manages: the system's configuration, not
#: whatever a development settings file points at.
SYSTEM_CONFIG_DIR = '/etc/mhvtl'
SYSTEM_HOME_DIR = '/opt/mhvtl'

COMMAND_MODULES = ('library', 'drive', 'tape', 'ltfs', 'operations', 'status',
                   'service', 'config', 'scsi', 'iscsi', 'console')


class _Version(argparse.Action):
    """``--version``, answered by the service rather than by this file.

    argparse's own ``action='version'`` wants the string when the parser is
    built, and build_parser() runs before Django is set up and must not import
    a service. So the import is deferred to the moment the flag is actually
    used. It is safe there because about.project() reads only
    mhvtl_system - five constants, no settings, no I/O - and formatting the
    answer here instead would be a second copy of what the About page shows.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        from apps.libraries.services import about
        print(about.one_line())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree.

    Built before Django is set up, so it must not import anything from the
    services. Command modules are imported here for their register() only;
    they import services inside their handlers.
    """
    parser = argparse.ArgumentParser(
        prog='mhvtl',
        description='Command line interface to the MHVTL tape library.')
    parser.add_argument('--version', action=_Version,
                        help="the console's version, author and licence")
    parser.add_argument('--json', action='store_true',
                        help='machine-readable output on stdout')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='print only what was asked for, no confirmations')
    parser.add_argument('--config-dir', metavar='DIR',
                        help=f'configuration directory to read '
                             f'(default {SYSTEM_CONFIG_DIR})')

    subparsers = parser.add_subparsers(dest='noun', metavar='<noun>')
    for name in COMMAND_MODULES:
        module = __import__(f'mhvtl_cli.commands.{name}', fromlist=['register'])
        register = getattr(module, 'register', None)
        if register is not None:
            register(subparsers)
    return parser


def setup_django(config_dir: Optional[str] = None) -> None:
    """Configure Django so the services can read their settings.

    Called after parsing, so --config-dir is in place before any service reads
    a path. Importing services before this raises ImproperlyConfigured, which is
    a worse error message than anything here.
    """
    from django.apps import apps

    if apps.ready:
        # Someone else configured Django - the test runner, or a management
        # command calling main() - and their settings stand. Overwriting them
        # here is process-wide: after one CLI test, every later test in the run
        # read /etc/mhvtl instead of the testing copy. Commands still honour
        # --config-dir, because they pass args.config_dir to each service.
        return

    sys.path.insert(0, str(_project_root()))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', DEFAULT_SETTINGS)

    import django
    django.setup()

    from django.conf import settings
    live = os.environ.get('MHVTL_CONFIG_DIR', SYSTEM_CONFIG_DIR)
    settings.MHVTL_CONFIG_DIR = config_dir or live
    settings.MHVTL_HOME_DIR = os.environ.get('MHVTL_HOME_DIR', SYSTEM_HOME_DIR)

    # Where the DAEMONS read, which --config-dir does not move. Without this,
    # a command given --config-dir had config_dir() pointing at the copy and
    # lifecycle.daemons_are_ours() comparing the copy against it - a directory
    # against itself, which answers yes. `ltfs add-media --config-dir <copy>`
    # then restarted the live vtllibrary@60 and unloaded a cartridge from under
    # a live `ltfs` mount. The same class of mistake once stopped three real
    # units, which is why this CLI reads /etc/mhvtl whatever the settings say.
    settings.MHVTL_DAEMON_CONFIG_DIR = live


def _project_root():
    """The directory holding manage.py - this package sits beside it."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def main(argv: List[str] = None) -> int:
    """Parse, set up Django, run one command, return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.noun or not getattr(args, 'handler', None):
        parser.print_help(sys.stderr)
        return output.EXIT_USAGE

    try:
        setup_django(args.config_dir)
    except Exception as exc:                           # noqa: BLE001 - reported
        return output.fail(f'could not start: {exc}',
                           'is this being run from the installed location?')

    try:
        return args.handler(args)
    except privileges.PermissionDenied as exc:
        return output.fail(str(exc), code=output.EXIT_DENIED)
    except KeyboardInterrupt:
        return output.fail('interrupted', code=output.EXIT_FAILED)
    except Exception as exc:                           # noqa: BLE001 - reported
        # A traceback is the right thing for a bug and the wrong thing for a
        # missing library, so services return failures rather than raising.
        # Anything that reaches here is a bug; say so and show it.
        import traceback
        traceback.print_exc()
        return output.fail(f'unexpected error: {exc}',
                           'this is a bug; the traceback is above')


if __name__ == '__main__':
    sys.exit(main())
