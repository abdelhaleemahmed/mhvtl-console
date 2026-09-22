"""Disk usage for the configuration and media directories.

Moved from console_service.py:469 (get_disk_usage) and :499
(get_mhvtl_disk_usage).

The tape count here counts media directories rather than files named `data`.
Counting the files worked until MHVTL 1.8 split media into data.0/indx.0/meta.0
per partition, after which the console reported 0 tapes on a host with 97 of
them - silently, because 0 is a plausible number.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core import QUICK, config_dir, home_dir, shell

logger = logging.getLogger(__name__)


@dataclass
class DiskUsage:
    """One filesystem, as df reports it."""
    path: str
    filesystem: str = ''
    size: str = ''
    used: str = ''
    available: str = ''
    percent: int = 0

    @property
    def size_gb(self) -> Optional[float]:
        """Total size in GB, from df's human-readable column."""
        return _to_gb(self.size)

    @property
    def used_gb(self) -> Optional[float]:
        return _to_gb(self.used)

    @property
    def available_gb(self) -> Optional[float]:
        return _to_gb(self.available)

    @property
    def level(self) -> str:
        """For colouring a bar: normal, warning or danger."""
        if self.percent >= 90:
            return 'danger'
        return 'warning' if self.percent >= 75 else 'normal'

    def to_dict(self) -> Dict:
        return {'path': self.path, 'filesystem': self.filesystem, 'size': self.size,
                'used': self.used, 'available': self.available,
                'size_gb': self.size_gb, 'used_gb': self.used_gb,
                'available_gb': self.available_gb,
                'percent': self.percent, 'level': self.level}


#: df -h writes 1.8T, 920G, 512M, 4.0K - a number and a unit letter.
SIZE_RE = re.compile(r'^([\d.]+)\s*([KMGTPE]?)i?B?$', re.IGNORECASE)

#: Powers of 1024 relative to a gigabyte, because df -h is binary despite the
#: single-letter suffix.
GB_MULTIPLIER = {'': 1 / 1024 ** 3, 'K': 1 / 1024 ** 2, 'M': 1 / 1024,
                 'G': 1.0, 'T': 1024.0, 'P': 1024.0 ** 2, 'E': 1024.0 ** 3}


def _to_gb(value: str) -> Optional[float]:
    """"920G" -> 920.0, "1.8T" -> 1843.2, "" -> None.

    A page showing a used bar needs numbers; df -h gives strings. Returning None
    for an unparseable one leaves the bar empty rather than showing a zero that
    reads as an empty disk.
    """
    match = SIZE_RE.match((value or '').strip())
    if not match:
        return None
    try:
        return round(float(match.group(1)) * GB_MULTIPLIER[match.group(2).upper()], 2)
    except (ValueError, KeyError):
        return None


def usage(paths: List[str] = None) -> List[DiskUsage]:
    """df for the directories MHVTL uses."""
    targets = [str(p) for p in (paths or [home_dir(), config_dir(), '/'])]
    result = shell.run(['df', '-h', *targets], timeout=QUICK)
    if not result.ok:
        logger.warning('df failed: %s', result.output.strip()[:200])
        return []

    found = []
    for line, path in zip(result.stdout.splitlines()[1:], targets):
        fields = line.split()
        if len(fields) < 5:
            continue
        percent = fields[4].rstrip('%')
        found.append(DiskUsage(path=path, filesystem=fields[0], size=fields[1],
                               used=fields[2], available=fields[3],
                               percent=int(percent) if percent.isdigit() else 0))
    return found


def media_usage() -> Dict:
    """How much space the tapes take, and how many there are."""
    root = home_dir()
    result = {'path': str(root), 'size': None, 'tape_count': 0}

    size = shell.sudo(['du', '-sh', str(root)], timeout=QUICK)
    if size.ok and size.stdout.split():
        result['size'] = size.stdout.split()[0]

    # One directory per tape, in both the 1.7 and 1.8 layouts.
    count = shell.sudo(['find', str(root), '-mindepth', '1', '-maxdepth', '1',
                        '-type', 'd'], timeout=QUICK)
    if count.ok:
        result['tape_count'] = len([line for line in count.stdout.splitlines() if line])

    return result
