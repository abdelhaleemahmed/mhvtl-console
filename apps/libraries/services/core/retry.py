"""Retrying SCSI operations that report a transient busy state.

Moved from tape_operations_service.py:1146 (_execute_mtx_with_retry), the only
retry logic in the project and worth keeping: the MHVTL robot reports "Logical
Unit Not Ready" for a second or two after a move, and a caller that gives up
immediately turns a working library into an intermittent one.

Two changes from the original:

    - the delay backs off instead of sleeping a flat 3 seconds five times. The
      original's worst case was 5 x 30s of command plus 5 x 3s of sleep, which
      can outlast the gunicorn worker timeout of 120s and kill the request that
      was waiting for it;
    - the decision to retry is a named predicate rather than an inline list, so
      the sense codes can be tested without running mtx.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import time
from typing import Callable

from .shell import CommandResult

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF = 1.5        # seconds, doubled each attempt: 1.5, 3, 6
MAX_BACKOFF = 8.0

#: Sense text that means "ask again shortly" rather than "this will never work".
#: ASC=04 is Logical Unit Not Ready; the robot reports it while it finishes a
#: previous move.
TRANSIENT_MARKERS = (
    'NOT READY',
    'LOGICAL UNIT NOT READY',
    'MANUAL INTERVENTION REQUIRED',
    'ASC=04',
    'DEVICE OR RESOURCE BUSY',
)


def is_transient(result: CommandResult) -> bool:
    """Is this failure worth retrying?

    A drive that is genuinely busy with a backup reports "Device or resource
    busy", and so does a robot mid-move; neither is permanent. A bad slot number
    or a missing device is not transient and retrying only delays the error.
    """
    if result.timed_out:
        return False                      # already waited; waiting again is worse
    text = f'{result.stderr}{result.stdout}'.upper()
    if 'HARDWARE ERROR' in text and 'MOVE MEDIUM' in text:
        return True                       # the robot was mid-move
    return any(marker in text for marker in TRANSIENT_MARKERS)


def retry_on_busy(operation: Callable[[], CommandResult], *,
                  attempts: int = DEFAULT_ATTEMPTS,
                  backoff: float = DEFAULT_BACKOFF,
                  description: str = 'command') -> CommandResult:
    """Run an operation, retrying while it reports a transient busy state.

        result = retry_on_busy(lambda: shell.sudo(['mtx', '-f', dev, 'status']),
                               description='mtx status')

    Returns the last result either way, so the caller sees the real error rather
    than a synthesised one.
    """
    delay = backoff
    result = operation()

    for attempt in range(2, attempts + 1):
        if result.ok or not is_transient(result):
            return result

        logger.info('%s reported a transient busy state; retrying in %.1fs '
                    '(attempt %d of %d)', description, delay, attempt, attempts)
        time.sleep(delay)
        delay = min(delay * 2, MAX_BACKOFF)
        result = operation()

    if not result.ok:
        logger.warning('%s still failing after %d attempts: %s',
                       description, attempts, result.output.strip()[:200])
    return result
