"""Which LTFS volumes are mounted, read from the kernel.

There is deliberately no state file here, and that is the whole design.

A FUSE mount outlives the request that created it, so the obvious approach is a
registry on disk - the shape core/samples.py uses for drive readings. It is the
wrong shape for this. A drive reading is a disposable hint: if the file is lost
the caller takes another reading. A mount is a real resource holding a real
drive, and a registry that disagrees with reality is worse than no registry:
it would claim a drive is busy after a reboot, or claim it is free while an
`ltfs` process holds it.

The kernel already keeps this state, it is already shared by every process, and
it is already correct after a crash or a reboot. /proc/mounts names the device
node in the mount source - `ltfs:/dev/sg22` - so a mount identifies its own
drive without anything having to remember. Everything else a caller wants
(which library, which cartridge) is composed from the mapping and the robot,
which are equally authoritative.

Rules for this module:
    - NO subprocess and no privilege: it reads one file the kernel maintains.
      The usual "all subprocess work goes through core.shell" does not apply
      because there is none, and that is the point of the module
    - returns plain values; a caller cannot tell a miss from a failure, because
      both mean "not mounted as far as we can see"
    - never imports django.contrib.messages and never sees a request
"""
import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: Where the kernel lists mounts. A file, so no privilege is needed to read it -
#: which is why mount state costs nothing and can be asked for on every page.
PROC_MOUNTS = Path('/proc/mounts')

#: ltfs mounts itself with this as the mount source, followed by the device
#: node: "ltfs:/dev/sg22". Anything else of type fuse is somebody else's.
SOURCE_PREFIX = 'ltfs:'

#: /proc/mounts escapes these in paths, octal, the way getmntent does.
_ESCAPES = {'\\040': ' ', '\\011': '\t', '\\012': '\n', '\\134': '\\'}


def _unescape(field: str) -> str:
    for code, character in _ESCAPES.items():
        field = field.replace(code, character)
    return field


def by_device(text: str = None) -> Dict[str, str]:
    """{device node: mount point} for every LTFS volume mounted right now.

    Reads /proc/mounts rather than running `mount`, so there is no subprocess
    and no privilege involved. A caller that cannot read it gets {} - the same
    answer as "nothing is mounted", which is the safe direction: the worst
    outcome is offering to mount something that is already mounted, and ltfs
    refuses that itself.
    """
    if text is None:
        try:
            text = PROC_MOUNTS.read_text()
        except OSError:
            logger.warning('cannot read %s; reporting no LTFS mounts', PROC_MOUNTS,
                           exc_info=True)
            return {}

    found: Dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        source, point, kind = fields[0], fields[1], fields[2]
        if not kind.startswith('fuse') or not source.startswith(SOURCE_PREFIX):
            continue
        node = source[len(SOURCE_PREFIX):]
        if node:
            found[node] = _unescape(point)
    return found


#: Every volume this console mounts goes to MOUNT_ROOT/library<N>-drive<M>
#: (service.mount_point_for), so the mount point says whose it is without
#: asking lsscsi which device node belongs to which drive. That matters: the
#: guard below is consulted whenever a drive is about to be emptied, and a
#: guard that costs a subprocess is a guard somebody will skip.
POINT_RE = re.compile(r'library(\d+)-drive(\d+)$')


def by_drive(library_id: int, text: str = None) -> Dict[int, str]:
    """{drive number: mount point} for this library's mounted volumes.

    Read from the mount POINT rather than the device node, so it needs no
    device discovery - one file read and a regular expression.

    The limit, stated because a guard's blind spot should not be a surprise:
    this sees volumes mounted at the paths this console uses. Someone who ran
    `ltfs` by hand against a different directory has a mounted filesystem this
    will not attribute to a drive. `by_device()` sees it; nothing can say which
    drive it belongs to without asking lsscsi.
    """
    found: Dict[int, str] = {}
    for point in by_device(text).values():
        match = POINT_RE.search(point)
        if match and int(match.group(1)) == int(library_id):
            found[int(match.group(2))] = point
    return found


def point_for(node: Optional[str], text: str = None) -> Optional[str]:
    """Where this device node is mounted, or None."""
    if not node:
        return None
    return by_device(text).get(node)

