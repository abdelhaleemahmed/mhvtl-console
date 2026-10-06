"""Settings for the `mhvtl` command line.

The third deployment context, and it needed saying out loud. The web service
runs under `production`; a checkout runs under `development`; the CLI is
neither, and running it under either was wrong in a different way:

    development  pulls in debug_toolbar, a development dependency that
                 `pip install .` does not install. The installed CLI only ever
                 worked because a stale virtualenv from an earlier release
                 still carried the development extras. Clean the virtualenv and
                 the CLI dies with "No module named 'debug_toolbar'".

    production   opens RotatingFileHandlers in /var/log/mhvtl/, which belongs to
                 the service account. A read-only command run by an
                 administrator - `mhvtl library list` - then fails before it
                 starts with "Unable to configure handler 'error_file'", which
                 says nothing about the real problem.

So: production in every respect except logging. The real directories, the real
database, DEBUG off, service control on - and diagnostics to stderr, where a
command line's diagnostics belong, so an unprivileged read works and a piped
command stays clean.

Nothing here decides anything about MHVTL. mhvtl_cli/main.py still overrides
MHVTL_CONFIG_DIR and MHVTL_DAEMON_CONFIG_DIR to the live /etc/mhvtl whatever
these settings say, for the reason written in its own docstring.
"""
from .production import *                                        # noqa: F401,F403

#: Diagnostics on stderr and nowhere else.
#:
#: A file handler here would have to be writable by whoever runs the command,
#: which is any administrator, so it would either fail for them or require a
#: world-writable log. The service's own log is the service's; a command's
#: output belongs to the terminal that asked for it.
#:
#: WARNING rather than INFO: the services log their steps at INFO, and a CLI
#: that printed its own internal progress alongside the output it was asked for
#: would not be pipeable. `--json` output in particular has to stay valid JSON.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'cli': {'format': '{levelname}: {message}', 'style': '{'},
    },
    'handlers': {
        'stderr': {
            'level': 'WARNING',
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stderr',
            'formatter': 'cli',
        },
    },
    'root': {'handlers': ['stderr'], 'level': 'WARNING'},
    'loggers': {
        'django': {'handlers': ['stderr'], 'level': 'WARNING',
                   'propagate': False},
        'apps': {'handlers': ['stderr'], 'level': 'WARNING',
                 'propagate': False},
        'mhvtl_cli': {'handlers': ['stderr'], 'level': 'WARNING',
                      'propagate': False},
    },
}
