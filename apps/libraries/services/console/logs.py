"""Reading log files and dmesg.

Moved from console_service.py:584 (get_logs), :643 (get_mhvtl_logs) and :658
(get_dmesg).

The original built a shell command string and ran it with shell=True, with the
caller's log_file interpolated into it - the only shell=True in the codebase.
Here the path is checked against an allowlist of known log locations and the
command is an argument list, so a path is a path and not a fragment of shell.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ..core import QUICK, shell

logger = logging.getLogger(__name__)

#: Logs this console will read. Anything else is refused: the name arrives from
#: a query string.
ALLOWED_LOGS = (
    '/var/log/mhvtl.log',
    '/var/log/messages',
    '/var/log/syslog',
    '/var/log/mhvtl/mhvtl.log',
    '/var/log/mhvtl/django.log',
    '/var/log/mhvtl/django_errors.log',
    '/var/log/mhvtl/mhvtl_operations.log',
    '/var/log/mhvtl/gunicorn-error.log',
    '/var/log/mhvtl/gunicorn-access.log',
)

DEFAULT_LINES = 100
MAX_LINES = 5000


def available() -> List[str]:
    """Allowlisted logs that exist on this host."""
    return [path for path in ALLOWED_LOGS if Path(path).exists()]


def is_allowed(path: str) -> bool:
    return str(path) in ALLOWED_LOGS


def read(path: str = None, lines: int = DEFAULT_LINES) -> Dict:
    """The last `lines` lines of an allowlisted log."""
    lines = max(1, min(int(lines or DEFAULT_LINES), MAX_LINES))

    if path is None:
        found = available()
        if not found:
            return {'success': False, 'log_file': None, 'lines': [],
                    'error': 'No MHVTL log file found in the expected locations'}
        path = found[0]

    if not is_allowed(path):
        logger.warning('refused a log path outside the allowlist: %s', path)
        return {'success': False, 'log_file': path, 'lines': [],
                'error': 'That log file is not one this console reads'}

    result = shell.sudo(['tail', '-n', str(lines), path], timeout=QUICK)
    if not result.ok:
        return {'success': False, 'log_file': path, 'lines': [],
                'error': result.output.strip()[:200] or 'could not read the log'}

    return {'success': True, 'log_file': path,
            'lines': result.stdout.splitlines(), 'count': lines}


def dmesg(lines: int = 50) -> Dict:
    """Recent kernel messages - where mhvtl.ko reports its own problems."""
    lines = max(1, min(int(lines or 50), MAX_LINES))
    result = shell.sudo(['dmesg', '--ctime'], timeout=QUICK)
    if not result.ok:
        return {'success': False, 'lines': [],
                'error': result.output.strip()[:200] or 'could not read dmesg'}
    return {'success': True, 'lines': result.stdout.splitlines()[-lines:]}


def mhvtl_messages(lines: int = DEFAULT_LINES) -> Dict:
    """Kernel messages mentioning mhvtl, which is usually what is wanted."""
    everything = dmesg(MAX_LINES)
    if not everything['success']:
        return everything
    matching = [line for line in everything['lines'] if 'mhvtl' in line.lower()]
    return {'success': True, 'lines': matching[-lines:]}
