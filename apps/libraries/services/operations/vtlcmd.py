"""The vtlcmd wrapper - MHVTL's message-queue control channel.

Moved from tape_operations_service.py:1379 (online/offline) and :1468 (MAP
commands), with one bug fixed on the way.

THE QUEUE ID IS THE DEVICE.CONF ID.

The original computed it:

    library_index = library_id // 10 if library_id >= 10 else library_id
    cmd = ['sudo', 'vtlcmd', str(library_index), command]

so library 10 was addressed as `vtlcmd 1`, library 20 as `vtlcmd 2`. There is no
queue 1 or 2 - the daemons listen on the id from device.conf, which is also the
systemd instance name (vtllibrary@10.service) and the -q argument the daemon was
started with. Online, offline and every MAP operation have therefore been talking
to a queue that does not exist.

The channel is worth understanding: vtlcmd does not go through SCSI. It posts to
a SysV message queue the daemon reads, which is why `stats` works while a backup
holds the drive and `mtx status` and `mt status` return "Device busy". That
`stats` verb is our own addition to MHVTL, upstream patch 0002.

A caveat measured on this host, not a theory: what a daemon reports is the
daemon's own idea of its state, and after a restart that can disagree with the
robot. Queue 21 - a library 20 drive - was observed reporting E01003L8, a
library 10 tape, while mtx showed every library 20 drive empty; it cleared on
the next real operation. This is the restart desync MHVTL has upstream, so treat
stats as a live progress indicator rather than as the authority on what is
loaded. mtx is the authority; stats is what you can read while mtx cannot.

For the same reason this module addresses drives by their device.conf id, and
offers no helper mapping an mtx drive index to a queue id: the two were observed
disagreeing while a daemon held stale state, and guessing that mapping is how an
operation reaches the wrong drive.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional

from ..core import NORMAL, CommandResult, shell

logger = logging.getLogger(__name__)

#: `vtlcmd <queue> stats` prints one line of key/value pairs. Present only on
#: builds carrying our patch; older daemons answer "Command for tape not allowed".
STATS_RE = re.compile(
    r'Tape:\s*(?P<barcode>\S+)\s+Loaded:\s*(?P<loaded>Yes|No)\s+'
    r'Written:\s*(?P<written>\d+)\s+Read:\s*(?P<read>\d+)\s+'
    r'WMedia:\s*(?P<wmedia>\d+)\s+RMedia:\s*(?P<rmedia>\d+)\s+'
    r'Capacity:\s*(?P<capacity>\d+)')


@dataclass
class TapeStats:
    """Live byte counters for a drive, read without touching SCSI."""
    barcode: Optional[str]
    loaded: bool
    written: int = 0
    read: int = 0
    written_media: int = 0
    read_media: int = 0
    capacity: int = 0

    @property
    def compression_ratio(self) -> Optional[float]:
        """Initiator bytes over media bytes: how well the data compressed."""
        if not self.written_media:
            return None
        return round(self.written / self.written_media, 2)

    @property
    def used_percent(self) -> Optional[float]:
        if not self.capacity:
            return None
        return round(self.written_media / self.capacity * 100, 1)

    def to_dict(self) -> Dict:
        return {
            'barcode': self.barcode,
            'loaded': self.loaded,
            'written': self.written,
            'read': self.read,
            'written_media': self.written_media,
            'read_media': self.read_media,
            'capacity': self.capacity,
            'compression_ratio': self.compression_ratio,
            'used_percent': self.used_percent,
        }


def send(queue_id: int, *command: str, timeout: int = NORMAL) -> CommandResult:
    """Send a command to a daemon's message queue.

    queue_id is the device.conf id - 10 for library 10, 21 for drive 21 - and
    never a derived index.
    """
    return shell.sudo(['vtlcmd', str(queue_id), *command], timeout=timeout)


def online(library_id: int) -> CommandResult:
    """Bring a library online."""
    return send(library_id, 'online')


def offline(library_id: int) -> CommandResult:
    """Take a library offline. Refused by the daemon while a drive has a tape."""
    return send(library_id, 'offline')


def open_map(library_id: int) -> CommandResult:
    return send(library_id, 'open', 'map')


def close_map(library_id: int) -> CommandResult:
    return send(library_id, 'close', 'map')


def load_map(library_id: int) -> CommandResult:
    """Have the robot re-read the MAP, so a tape put in it becomes visible."""
    return send(library_id, 'load', 'map')


def list_map(library_id: int) -> CommandResult:
    return send(library_id, 'list', 'map')


def empty_map(library_id: int) -> CommandResult:
    return send(library_id, 'empty', 'map')


def parse_stats(text: str) -> Optional[TapeStats]:
    """Parse one `vtlcmd <drive> stats` line, or None if it is not one."""
    match = STATS_RE.search(text or '')
    if not match:
        return None

    barcode = match.group('barcode')
    return TapeStats(
        barcode=None if barcode in ('N/A', '') else barcode,
        loaded=match.group('loaded') == 'Yes',
        written=int(match.group('written')),
        read=int(match.group('read')),
        written_media=int(match.group('wmedia')),
        read_media=int(match.group('rmedia')),
        capacity=int(match.group('capacity')))


def stats(drive_id: int) -> Optional[TapeStats]:
    """Live counters for a drive, even while a backup is writing to it.

    Returns None when the daemon does not know the verb - MHVTL builds without
    our patch answer "Command for tape not allowed".
    """
    result = send(drive_id, 'stats')
    if not result.ok:
        logger.info('vtlcmd %s stats: %s', drive_id, result.output.strip()[:120])
        return None
    return parse_stats(result.output)
