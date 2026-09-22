"""The only place this project runs a subprocess.

Lifted from the two implementations worth keeping: the sudo helpers in
mhvtl_library_service.py:187-213 (_sudo_cat/_sudo_cp/_sudo_tee) and the typed,
list-argument runner in iscsi_service.py:206. Replaces four competing dialects,
including console_service.py:133 - the only shell=True in the codebase, which
interpolated a caller-supplied log path into a shell command.

Timeouts were previously chosen per call site: 30 in 33 places, 10 in 18, 5 in
11, 60 in 10, 600 in 2. They are named here instead, so "how long should mtx get"
is answered once.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

#: Named timeouts, in seconds. Anything slower than SLOW belongs in a background
#: job rather than a request: the gunicorn worker gives up at 120.
QUICK = 10      # lsscsi, systemctl is-active, reading a config file
NORMAL = 30     # mtx status, vtlcmd, mktape
SLOW = 60       # service restarts, targetcli
DEFAULT_TIMEOUT = NORMAL


@dataclass
class CommandResult:
    """What a command did. Never raises on a non-zero exit; callers decide."""
    argv: List[str] = field(default_factory=list)
    returncode: int = 0
    stdout: str = ''
    stderr: str = ''
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """stdout, or stderr when the command said nothing on stdout."""
        return self.stdout if self.stdout.strip() else self.stderr

    def to_dict(self) -> dict:
        return {
            'argv': self.argv,
            'returncode': self.returncode,
            'stdout': self.stdout,
            'stderr': self.stderr,
            'timed_out': self.timed_out,
            'ok': self.ok,
        }


def run(argv: Sequence[Union[str, Path]], *, timeout: int = DEFAULT_TIMEOUT,
        input_text: Optional[str] = None) -> CommandResult:
    """Run a command and report what happened.

    argv is always a list, so there is no shell and nothing in it can be read as
    a shell metacharacter. A timeout is always set: a wedged mtx call otherwise
    holds a worker until gunicorn kills it.
    """
    args = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, input=input_text)
        result = CommandResult(args, completed.returncode,
                               completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired:
        logger.warning('timed out after %ss: %s', timeout, ' '.join(args))
        return CommandResult(args, -1, '', f'timed out after {timeout}s', timed_out=True)
    except FileNotFoundError as exc:
        logger.warning('command not found: %s', args[0])
        return CommandResult(args, -1, '', str(exc))

    if not result.ok:
        logger.debug('exit %s: %s | %s', result.returncode, ' '.join(args),
                     result.stderr.strip()[:200])
    return result


def sudo(argv: Sequence[Union[str, Path]], *, timeout: int = DEFAULT_TIMEOUT,
         input_text: Optional[str] = None) -> CommandResult:
    """Run a command through sudo.

    Everything MHVTL owns - the config directory, the media directory, the
    daemons - is root-owned, so mutations go through the sudoers rules shipped in
    packaging/rpm/mhvtl-gui.sudoers.
    """
    return run(['sudo', *argv], timeout=timeout, input_text=input_text)


def sudo_bytes(argv: Sequence[Union[str, Path]], *,
               timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """Run a command through sudo and keep its output as bytes.

    run() decodes to text, which is right for every command that prints words
    and wrong for one that prints a file: replacing undecodable bytes silently
    corrupts what was read. Returns (ok, stdout bytes, stderr text).
    """
    args = ['sudo', *[str(part) for part in argv]]
    try:
        completed = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning('timed out after %ss: %s', timeout, ' '.join(args))
        return False, b'', f'timed out after {timeout}s'
    except FileNotFoundError as exc:
        logger.warning('command not found: %s', args[1] if len(args) > 1 else args[0])
        return False, b'', str(exc)

    stderr = completed.stderr.decode('utf-8', errors='replace')
    if completed.returncode != 0:
        logger.debug('exit %s: %s | %s', completed.returncode, ' '.join(args),
                     stderr.strip()[:200])
    return completed.returncode == 0, completed.stdout, stderr


def sudo_cat(path: Union[str, Path], *, timeout: int = QUICK) -> CommandResult:
    """Read a possibly root-only file, trying a plain read first.

    Most config files are world-readable; library_contents.20 on this host is
    mode 600. Reading without sudo when possible keeps read-only CLI commands
    usable by an unprivileged account.
    """
    try:
        return CommandResult([str(path)], 0, Path(path).read_text(errors='replace'), '')
    except PermissionError:
        return sudo(['cat', path], timeout=timeout)
    except OSError as exc:
        return CommandResult([str(path)], -1, '', str(exc))


def sudo_tee(path: Union[str, Path], text: str, *, timeout: int = QUICK) -> CommandResult:
    """Write a root-owned file through tee.

    For config files prefer core.locking.atomic_write_text, which locks and
    renames instead of truncating in place.
    """
    return sudo(['tee', path], timeout=timeout, input_text=text)
