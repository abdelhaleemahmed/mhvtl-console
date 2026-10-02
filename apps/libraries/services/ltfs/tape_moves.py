"""Taking a cartridge out of a drive, when a filesystem may be open on it.

WHY THIS LIVES IN ltfs/ AND NOT IN operations/
----------------------------------------------
Moving a tape is an operations job and the guard belongs with the operation -
but `operations/` may not import `ltfs/`. Rule 7 in services/__init__.py: the
graph is a DAG, `ltfs` is a leaf that nothing imports, and `ltfs` already
imports `operations`. An edge the other way closes a cycle, and
tests/test_service_layers.py fails on it.

So the guarded operation lives on the side that can see both. `ltfs/` knows
which drives hold a mounted filesystem and can call OperationsService; the
reverse was never going to work without either inverting the graph or putting
a second copy of the mount-point convention into `operations/`, which is the
duplication this codebase has spent a lot of effort removing.

`OperationsService.unmount()` is still there and still unguarded - it is the
mechanism. `unmount_tape()` below is the operation an operator asks for, and
both callers, the mount page and `mhvtl op unmount`, go through it.

WHY IT REFUSES RATHER THAN WARNS
--------------------------------
A cartridge whose filesystem is mounted is a cartridge something is reading or
writing. Pulling it out of the drive does not close the filesystem; it leaves
`ltfs` holding a device with no medium, and whatever was using the mount gets
I/O errors rather than a clean end. During this work a daemon restart did
exactly that, and nothing stopped it because nothing on the path knew.

The order is not a matter of taste: unmount the filesystem, then unload the
tape. So this is a failure_result, not a flag on a success.
"""
import logging
from typing import Dict, Optional

from ..core import ServiceResult, failure_result
from ..operations.service import OperationsService
from . import mounts

logger = logging.getLogger(__name__)


def blocked_drives(library_id: int, text: str = None) -> Dict[int, str]:
    """{drive number: why it may not be emptied} for this library.

    One /proc/mounts read, no privilege and no subprocess, so a caller can ask
    on every page load. An empty dict means every drive may be unloaded.

    If /proc/mounts cannot be read at all, `mounts.by_device()` reports nothing
    mounted and this allows the unload. That is the unsafe direction and it is
    chosen deliberately: on Linux that file is always readable, and refusing
    every unmount because we cannot see the mount table would leave an operator
    unable to work for a reason that has nothing to do with their tape.
    """
    return {drive: f'an LTFS filesystem is mounted on it, at {point}'
            for drive, point in mounts.by_drive(library_id, text).items()}


def unmount_tape(library_id: int, drive: int, slot: Optional[int] = None,
                 *, config_directory=None) -> ServiceResult:
    """Return the cartridge in `drive` to a slot, unless a filesystem is open.

    The one entry point an operator's unmount should take. `slot` defaults to
    the cartridge's own origin, which is what OperationsService.unmount() does
    and what makes the second dropdown on the old unmount page unnecessary.
    """
    blocked = blocked_drives(library_id)
    reason = blocked.get(int(drive))
    if reason:
        logger.info('refused to unload drive %s of library %s: %s',
                    drive, library_id, reason)
        return failure_result(
            f'Drive {drive} cannot be emptied: {reason}',
            ['Unmount the filesystem first - the LTFS page, or '
             f'`mhvtl ltfs unmount {library_id} {drive}` - and then unload the '
             'tape.',
             'Pulling the cartridge out from under a mounted filesystem gives '
             'whatever is using it I/O errors rather than a clean end.'],
            operation_id='unmount_tape')

    return OperationsService(config_directory).unmount(library_id, int(drive),
                                                       slot)
