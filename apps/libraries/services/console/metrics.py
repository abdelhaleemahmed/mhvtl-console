"""Per-library metrics: is the daemon up, what is it costing, and where.

Moved from monitoring_service.py (get_service_status, _get_service_uptime,
get_process_metrics, get_disk_metrics, get_library_metrics, _count_online_drives).
That module was one of five places asking systemctl whether a unit was active;
the other four have already collapsed onto units.py, and this is the last.

What it answers, for one library:

    the robot daemon's unit state, and how long it has been up
    that process's CPU, resident memory and share of the machine
    the media directory's disk usage
    how many of the library's drive daemons are running

The drive count is the part worth stating. The version this replaces took the
number of drives from the Django database and then assumed their ids ran
library_id+1..n, so a library whose drives were added and removed over time
reported the wrong number, and a library the database disagreed with reported a
number for drives that do not exist. The ids come from device.conf here.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from ..core import QUICK, shell
from . import disk, units

logger = logging.getLogger(__name__)


@dataclass
class ProcessMetrics:
    """What one daemon process is costing."""
    pid: int
    cpu_percent: float
    memory_percent: float
    memory_mb: float

    def to_dict(self) -> Dict:
        return {'pid': self.pid, 'cpu_percent': self.cpu_percent,
                'memory_percent': self.memory_percent,
                'memory_mb': self.memory_mb}


@dataclass
class LibraryMetrics:
    """One library's daemon, its cost, its disk and its drives."""
    library_id: int
    unit: str
    running: bool
    state: str
    uptime: Optional[str] = None
    process: Optional[ProcessMetrics] = None
    disk_usage: Optional[disk.DiskUsage] = None
    drives_online: int = 0
    drives_total: int = 0

    @property
    def healthy(self) -> bool:
        """Running, with every one of its drives running too."""
        return self.running and self.drives_online == self.drives_total

    def to_dict(self) -> Dict:
        return {
            'library_id': self.library_id,
            'unit': self.unit,
            'running': self.running,
            'state': self.state,
            'healthy': self.healthy,
            'uptime': self.uptime,
            'process': self.process.to_dict() if self.process else None,
            'disk': self.disk_usage.to_dict() if self.disk_usage else None,
            'drives_online': self.drives_online,
            'drives_total': self.drives_total,
        }


def uptime(unit: str) -> Optional[str]:
    """How long a unit has been active, as "3d 4h" or "12m".

    None when systemd has no start time for it, which is the honest answer for a
    unit that has never run. The version this replaces returned the string
    "Running" in that case, so a stopped daemon's page said Running.
    """
    result = shell.run(['systemctl', 'show', unit,
                        '--property=ActiveEnterTimestamp', '--value'],
                       timeout=QUICK)
    stamp = result.stdout.strip()
    if not result.ok or not stamp or stamp == 'n/a':
        return None

    # systemd prints "Mon 2026-01-26 10:30:00 UTC"; the zone is the local one.
    parts = stamp.split()
    if len(parts) < 3:
        return None
    try:
        started = datetime.strptime(' '.join(parts[:3]), '%a %Y-%m-%d %H:%M:%S')
    except ValueError:
        logger.warning('cannot read a start time from %r', stamp)
        return None

    delta = datetime.now() - started
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    if delta.days:
        return f'{delta.days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'


def process(pid: int) -> Optional[ProcessMetrics]:
    """CPU, memory share and resident size for one pid.

    Read with ps rather than /proc arithmetic: ps already computes the
    percentages the same way top does, and the numbers on the page should match
    what an operator sees in a terminal.
    """
    result = shell.run(['ps', '-p', str(int(pid)), '-o', '%cpu,%mem,rss',
                        '--no-headers'], timeout=QUICK)
    fields = result.stdout.split()
    if not result.ok or len(fields) < 3:
        return None
    try:
        return ProcessMetrics(pid=int(pid), cpu_percent=round(float(fields[0]), 1),
                              memory_percent=round(float(fields[1]), 1),
                              memory_mb=round(int(fields[2]) / 1024, 1))
    except ValueError:
        logger.warning('cannot read ps output %r for pid %s', result.stdout, pid)
        return None


def for_library(library_id: int, drive_ids: List[int] = None,
                config_dir=None) -> LibraryMetrics:
    """Everything worth showing on one library's monitoring page.

    Args:
        library_id: the device.conf id.
        drive_ids: its drives. Read from device.conf when not given, which is
            the authority; passing them in is for a caller that has already
            parsed it.
    """
    library_id = int(library_id)
    unit = f'vtllibrary@{library_id}.service'

    if drive_ids is None:
        drive_ids = _drive_ids(library_id, config_dir)

    state = units._is(unit, 'is-active')
    running = state == 'active'

    process_metrics = None
    if running:
        pids = units.pids_of(unit)
        process_metrics = process(pids[0]) if pids else None

    disk_usage = None
    usages = disk.usage()
    if usages:
        disk_usage = usages[0]

    online = sum(1 for drive_id in drive_ids
                 if units.is_active(f'vtltape@{drive_id}.service'))

    return LibraryMetrics(
        library_id=library_id, unit=unit, running=running, state=state,
        uptime=uptime(unit) if running else None, process=process_metrics,
        disk_usage=disk_usage, drives_online=online, drives_total=len(drive_ids))


def _drive_ids(library_id: int, config_dir=None) -> List[int]:
    """The library's drive ids, from device.conf.

    An empty list when the file cannot be read: reporting nothing is better than
    reporting drives at guessed ids, which is what the version this replaces did.
    """
    from ..config.service import ConfigService

    conf = ConfigService(config_dir).device_conf()
    if conf is None:
        logger.warning('cannot read device.conf for library %s metrics', library_id)
        return []
    return sorted(conf.drives_of(library_id))
