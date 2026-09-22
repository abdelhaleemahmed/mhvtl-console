"""Handing out library ids, drive ids and SCSI targets.

MHVTL puts every library and every drive in one id namespace: the id is the
daemon's message queue id and the kernel minor number, and both daemons refuse
anything at or above 1024 (see profiles/personalities.MAX_DEVICE_ID for the
source lines). A drive's id does not have to relate to its library's - the
drive's `Library ID: N Slot: M` line is what ties it to the library
(usr/cmd/vtllibrary.c:634) - so the only hard rule is that no two devices share
an id.

The conventions this module keeps on top of that rule are for people::

    libraries   the lowest free multiple of ten - 10, 20, 30 ...
    drives      library id + slot while that is free and not a multiple of ten,
                so drive 11 still reads as library 10's first drive; otherwise
                the next free id that is not a multiple of ten, which leaves
                future library ids available

The previous allocators used library id + slot unconditionally and checked it
only against other drives, so a tenth drive on library 10 was given id 20 -
library 20's id.

Targets: one per device, LUN 0. MHVTL itself sets no target limit (the daemons
parse TARGET unchecked and __scsi_add_device ignores the host's max_id);
MAX_TARGET in device_conf caps it at 99 so the NAA line keeps two-digit fields.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Iterable, List, Optional, Set, Tuple

from ..profiles.personalities import MAX_DEVICE_ID
from . import device_conf as device_conf_format

logger = logging.getLogger(__name__)

#: Library ids are multiples of this, and drive ids avoid them.
LIBRARY_STEP = 10


class OutOfIds(ValueError):
    """No id or target is left for what was asked."""


def used_ids(conf) -> Set[int]:
    """Every id device.conf already gives a library or a drive."""
    return set(conf.libraries) | set(conf.drives)


def next_library_id(conf, *, step: int = LIBRARY_STEP,
                    reserved: Iterable[int] = ()) -> Optional[int]:
    """The lowest free multiple of `step`, or None when there is none.

    Free means used by neither a library nor a drive: a drive that took id 40
    before this rule existed makes 40 unavailable as a library id.
    """
    taken = used_ids(conf) | set(reserved)
    candidate = step
    while candidate <= MAX_DEVICE_ID:
        if candidate not in taken:
            return candidate
        candidate += step
    return None


def _usable_drive_id(candidate: int, taken: Set[int]) -> bool:
    return (0 < candidate <= MAX_DEVICE_ID and candidate not in taken
            and candidate % LIBRARY_STEP != 0)


def drive_ids(conf, library_id: int, slots: Iterable[int], *,
              reserved: Iterable[int] = ()) -> List[int]:
    """An id for each slot, preferring library id + slot.

    `reserved` holds ids already promised in the same operation - the library's
    own id when it is being created. Raises OutOfIds when the namespace runs
    out, naming how many were wanted.
    """
    library_id = int(library_id)
    taken = used_ids(conf) | set(reserved) | {library_id}
    chosen = []
    slots = list(slots)

    for slot in slots:
        preferred = library_id + int(slot)
        if _usable_drive_id(preferred, taken):
            chosen.append(preferred)
            taken.add(preferred)
            continue

        # Search upward from the library first, so a drive lands near the
        # library it belongs to, then wrap to the bottom of the range.
        search = list(range(library_id + 1, MAX_DEVICE_ID + 1)) + \
            list(range(1, library_id))
        found = next((candidate for candidate in search
                      if _usable_drive_id(candidate, taken)), None)
        if found is None:
            raise OutOfIds(
                f'no free drive id for {len(slots)} drive(s): ids 1-'
                f'{MAX_DEVICE_ID} that are not multiples of {LIBRARY_STEP} are '
                f'all in use')
        chosen.append(found)
        taken.add(found)
    return chosen


def free_targets(conf) -> int:
    """How many SCSI targets are left: a new library needs one for itself and
    one per drive, so this less one is the most drives it can have."""
    used = conf.used_targets()
    return sum(1 for target in range(device_conf_format.MAX_TARGET + 1)
               if target not in used)


def targets(conf, count: int) -> Tuple[int, ...]:
    """`count` free SCSI targets, contiguous if possible.

    Contiguous above the highest target in use first, which keeps one library's
    devices together the way the existing allocation did; then contiguous
    anywhere; then any free targets at all, since MHVTL does not need them
    together. Raises OutOfIds when fewer than `count` are free below MAX_TARGET.
    """
    count = int(count)
    used = conf.used_targets()
    limit = device_conf_format.MAX_TARGET
    free = [target for target in range(limit + 1) if target not in used]
    if len(free) < count:
        raise OutOfIds(
            f'{count} SCSI target(s) needed, {len(free)} free in 0-{limit}')

    start = (max(used) + 1) if used else 0
    if start + count - 1 <= limit:
        return tuple(range(start, start + count))

    for index in range(len(free) - count + 1):
        block = free[index:index + count]
        if block[-1] - block[0] == count - 1:
            return tuple(block)

    return tuple(free[:count])


def plan_library(conf, library_id: int, num_drives: int) -> Tuple[int, List[int],
                                                                 List[int]]:
    """Everything a new library needs: (library target, drive ids, drive targets).

    Raises OutOfIds if any of it cannot be had, before anything is written.
    """
    wanted = targets(conf, int(num_drives) + 1)
    ids = drive_ids(conf, library_id, range(1, int(num_drives) + 1))
    return wanted[0], ids, list(wanted[1:])


def empty_conf():
    """A parsed device.conf with nothing in it, for planning on a new host."""
    return device_conf_format.parse('')
