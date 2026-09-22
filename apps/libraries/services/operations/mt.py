"""The mt wrapper - drive-level status.

Moved from tape_operations_service.py:985. mt talks to the tape device itself
(/dev/st*, or /dev/nst* to avoid rewinding on close), so it answers questions
mtx cannot: what density the loaded media is, whether the drive is at BOT, how
many soft errors it has seen.

It goes through SCSI, so it fails with "Device or resource busy" while a backup
holds the drive. When that happens the answer is vtlcmd stats, not a retry.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Optional

from ..core import QUICK, shell

logger = logging.getLogger(__name__)

FILE_BLOCK_RE = re.compile(r'File number=(-?\d+),\s*block number=(-?\d+)')
DENSITY_RE = re.compile(r'Density code\s+(0x[0-9a-fA-F]+)\s*(?:\(([^)]*)\))?')
BLOCK_SIZE_RE = re.compile(r'Tape block size (\d+) bytes')
BUSY_MARKERS = ('device or resource busy', 'no medium found')

#: The flag names on mt's "General status bits" line: WR_PROT, BOT, EOT,
#: DR_OPEN, ONLINE, IM_REP_EN. Matched as whole words because the same output
#: carries the hex status value, and a substring search for BOT finds it there.
FLAG_RE = re.compile(r'\b([A-Z][A-Z0-9_]{2,})\b')


@dataclass
class DriveStatus:
    """What mt reports about a drive."""
    device: str
    online: bool = False
    busy: bool = False
    file_number: Optional[int] = None
    block_number: Optional[int] = None
    block_size: Optional[int] = None
    density_code: Optional[str] = None
    density_name: Optional[str] = None
    #: From the "General status bits on" line, which mt prints as flag names.
    write_protected: bool = False
    at_bot: bool = False
    at_eot: bool = False
    door_open: bool = False
    raw: str = ''

    @property
    def ready(self) -> bool:
        """Loaded and usable: online with the door shut."""
        return self.online and not self.door_open

    @property
    def has_medium(self) -> bool:
        """A drive with no tape reports file/block -1."""
        return self.online and self.file_number is not None and self.file_number >= 0

    def to_dict(self) -> Dict:
        return {
            'device': self.device,
            'online': self.online,
            'busy': self.busy,
            'has_medium': self.has_medium,
            'file_number': self.file_number,
            'block_number': self.block_number,
            'block_size': self.block_size,
            'density_code': self.density_code,
            'density_name': self.density_name,
            'ready': self.ready,
            'write_protected': self.write_protected,
            'at_bot': self.at_bot,
            'at_eot': self.at_eot,
            'door_open': self.door_open,
        }


def non_rewinding(device_path: str) -> str:
    """The node that does not rewind the tape when it is closed.

    /dev/stN rewinds on close; /dev/nstN is the same drive without that. It
    matters for reading as much as for writing: `mt -f /dev/st0 status` closes
    the device and therefore moves the tape, so asking a drive what it is doing
    changes what it is doing. Two `mhvtl status drive` runs a minute apart
    reported different positions for that reason.

    Falls back to whatever was passed when there is no non-rewinding node -
    a /dev/sg node, or a host that does not create them.
    """
    if device_path and device_path.startswith('/dev/st'):
        candidate = device_path.replace('/dev/st', '/dev/nst', 1)
        if Path(candidate).exists():
            return candidate
    return device_path


def parse(device: str, text: str) -> DriveStatus:
    """Parse `mt status` output."""
    status = DriveStatus(device=device, raw=text)
    lowered = (text or '').lower()

    if any(marker in lowered for marker in BUSY_MARKERS):
        status.busy = 'busy' in lowered
        return status

    status.online = 'drive' in lowered or 'file number' in lowered

    match = FILE_BLOCK_RE.search(text)
    if match:
        status.file_number = int(match.group(1))
        status.block_number = int(match.group(2))

    match = BLOCK_SIZE_RE.search(text)
    if match:
        status.block_size = int(match.group(1))

    match = DENSITY_RE.search(text)
    if match:
        status.density_code = match.group(1)
        status.density_name = (match.group(2) or '').strip() or None

    # The flag names mt prints under "General status bits on (41010000):".
    # Matched as whole words: the raw output also contains the hex value, and
    # substring matching found BOT inside it.
    flags = set(FLAG_RE.findall(text or ''))
    status.write_protected = 'WR_PROT' in flags
    status.at_bot = 'BOT' in flags
    status.at_eot = 'EOT' in flags
    status.door_open = 'DR_OPEN' in flags

    return status


def status(device: str) -> DriveStatus:
    """Read a drive's status. Not retried: a busy drive stays busy for a while.

    Read through the non-rewinding node, so asking does not move the tape.
    """
    device = non_rewinding(device)
    result = shell.sudo(['mt', '-f', device, 'status'], timeout=QUICK)
    text = result.output
    if not result.ok:
        logger.info('mt status on %s: %s', device, text.strip()[:120])
    return parse(device, text)
