"""Host facts: kernel, uptime, load, memory.

Moved from console_service.py:158 (get_system_info). Each fact is read with its
own argument-list command rather than a shell pipeline, so a failure in one does
not take the page down with it.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from dataclasses import dataclass
from typing import Dict

from ..core import QUICK, shell

logger = logging.getLogger(__name__)


@dataclass
class SystemInfo:
    """What the console header shows about the host."""
    hostname: str = ''
    kernel: str = ''
    distribution: str = ''
    #: The processor, as /proc/cpuinfo names it, and how many the host has.
    #: The console asked for both from the start; nothing provided them, so
    #: the page read "CPU Unknown ( cores)".
    cpu_model: str = ''
    cpu_count: int = 0
    uptime: str = ''
    load_average: str = ''
    memory_total: str = ''
    memory_used: str = ''
    #: Share of memory in use, 0-100, from /proc/meminfo (MemTotal minus
    #: MemAvailable). The console dashboard and its refresh endpoint read it;
    #: it was missing, so the page showed 0% and the endpoint failed.
    memory_percent: float = 0.0

    @property
    def memory_level(self) -> str:
        return memory_level(self.memory_percent)

    def to_dict(self) -> Dict:
        return {'hostname': self.hostname, 'kernel': self.kernel,
                'distribution': self.distribution, 'cpu_model': self.cpu_model,
                'cpu_count': self.cpu_count, 'uptime': self.uptime,
                'load_average': self.load_average,
                'memory_total': self.memory_total, 'memory_used': self.memory_used,
                'memory_percent': self.memory_percent,
                'memory_level': self.memory_level}


def _first_line(argv) -> str:
    result = shell.run(argv, timeout=QUICK)
    return result.stdout.strip().splitlines()[0] if result.ok and result.stdout.strip() else ''


def info() -> SystemInfo:
    """Read the host facts. Anything unavailable is left blank, not guessed."""
    system = SystemInfo(
        hostname=_first_line(['hostname']),
        kernel=_first_line(['uname', '-r']),
        uptime=_first_line(['uptime', '-p']))

    release = shell.run(['cat', '/etc/os-release'], timeout=QUICK)
    if release.ok:
        for line in release.stdout.splitlines():
            if line.startswith('PRETTY_NAME='):
                system.distribution = line.split('=', 1)[1].strip().strip('"')
                break

    processors = shell.run(['cat', '/proc/cpuinfo'], timeout=QUICK)
    if processors.ok:
        system.cpu_model, system.cpu_count = cpu(processors.stdout)

    load = shell.run(['cat', '/proc/loadavg'], timeout=QUICK)
    if load.ok:
        system.load_average = ' '.join(load.stdout.split()[:3])

    memory = shell.run(['free', '-h'], timeout=QUICK)
    if memory.ok:
        for line in memory.stdout.splitlines():
            if line.lower().startswith('mem:'):
                fields = line.split()
                if len(fields) >= 3:
                    system.memory_total, system.memory_used = fields[1], fields[2]
                break

    meminfo = shell.run(['cat', '/proc/meminfo'], timeout=QUICK)
    if meminfo.ok:
        system.memory_percent = memory_percent(meminfo.stdout)

    return system


def cpu(cpuinfo: str):
    """(model, count) from /proc/cpuinfo text.

    The count is the number of `processor` lines - logical CPUs, which is what
    the host schedules on and what `nproc` reports. ARM kernels give no `model
    name`, so the model can be blank while the count is right.
    """
    model, count = '', 0
    for line in cpuinfo.splitlines():
        name, _, rest = line.partition(':')
        name = name.strip()
        if name == 'processor':
            count += 1
        elif name in ('model name', 'Model', 'cpu model') and not model:
            model = rest.strip()
    return model, count


def memory_percent(meminfo: str) -> float:
    """Percent of memory in use from /proc/meminfo text, one decimal."""
    values = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(':')
        if rest.split():
            values[name] = int(rest.split()[0])
    total, available = values.get('MemTotal'), values.get('MemAvailable')
    if not total or available is None:
        return 0.0
    return round(100.0 * (total - available) / total, 1)


#: Memory in use above which the console warns, and above which it alarms.
#: These are the values the dashboard template used when it decided this
#: itself - strictly above 60 and 80 - moved here unchanged, so the page and
#: `mhvtl console system` give the same answer. Disks and tapes use 75/90;
#: whether memory should too is a separate decision.
MEMORY_WARNING_ABOVE = 60.0
MEMORY_DANGER_ABOVE = 80.0


def memory_level(percent: float) -> str:
    """normal, warning or danger - a class name the page colours, not a colour."""
    if percent > MEMORY_DANGER_ABOVE:
        return 'danger'
    if percent > MEMORY_WARNING_ABOVE:
        return 'warning'
    return 'normal'


#: The programs a working MHVTL install provides.
REQUIRED_PROGRAMS = ('vtllibrary', 'vtltape', 'vtlcmd', 'mktape')


def mhvtl_installation(bin_dir: str = '/usr/bin') -> Dict:
    """Is MHVTL installed, and is its kernel side loaded and its target up?

    Moved from the script adapter's check_mhvtl_availability. A host can have
    every program installed and still run no library - no backend module, or
    the target stopped - so all three are reported.
    """
    import os
    from pathlib import Path

    from ..core import config_dir, home_dir
    from . import modules, units

    status = {'available': True, 'scripts': {}, 'errors': [],
              'config_dir_exists': Path(config_dir()).exists(),
              'data_dir_exists': Path(home_dir()).exists()}

    for program in REQUIRED_PROGRAMS:
        path = Path(bin_dir) / program
        exists = path.exists()
        executable = exists and os.access(path, os.X_OK)
        status['scripts'][program] = {'exists': exists, 'executable': executable,
                                      'path': str(path)}
        if not executable:
            status['available'] = False
            status['errors'].append(f'{program} is '
                                    f'{"not executable" if exists else "not installed"}')

    try:
        backend = modules.summary().get('backend')
        running = units.status().target_active
    except Exception as exc:                           # noqa: BLE001 - reported
        status['errors'].append(f'could not read the running state: {exc}')
        return status

    status.update(backend=backend, module_loaded=backend != 'none',
                  service_running=running)
    if backend == 'none':
        status['available'] = False
        status['errors'].append('no MHVTL backend is loaded: neither mhvtl.ko nor '
                                'the TCMU modules')
    if not running:
        status['errors'].append('mhvtl.target is not running')
    return status
