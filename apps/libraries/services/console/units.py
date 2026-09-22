"""systemd state for the MHVTL units, and starting and stopping them.

Moved from console_service.py:228 (get_mhvtl_service_status) and :346
(control_mhvtl_service). It replaces five separate is-active/is-enabled
implementations: monitoring_service.py:129, mhvtl_library_service.py:1588,
ajax_views.py:532 and management/commands/mhvtl_service.py:143 all had their own.

It also provides the get_service_status() that views.py:181 and
iscsi_views.py:446 have been calling on MHVTLLibraryService, which does not
define it - the AttributeError was caught and reported as "MHVTL unavailable" on
a healthy host.

The unit hierarchy is worth stating once:

    mhvtl.target                  what an operator starts and stops
    mhvtl-load-modules.service    loads mhvtl.ko, or the TCMU modules on 1.8
    vtllibrary@<id>.service       one robot daemon per library
    vtltape@<id>.service          one drive daemon per drive

The instance ids are the device.conf ids, which is the same rule vtlcmd follows.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import QUICK, SLOW, shell

logger = logging.getLogger(__name__)

TARGET = 'mhvtl.target'
MODULES_UNIT = 'mhvtl-load-modules.service'
LIBRARY_UNIT_RE = re.compile(r'^(vtllibrary@\d+\.service)')
TAPE_UNIT_RE = re.compile(r'^(vtltape@\d+\.service)')


@dataclass
class UnitState:
    """One systemd unit."""
    name: str
    active: bool = False
    state: str = 'unknown'

    def to_dict(self) -> Dict:
        return {'name': self.name, 'active': self.active, 'state': self.state}


@dataclass
class MhvtlServiceStatus:
    """The whole MHVTL unit hierarchy in one answer."""
    target_active: bool = False
    target_enabled: bool = False
    modules_loaded: bool = False
    libraries: List[UnitState] = field(default_factory=list)
    drives: List[UnitState] = field(default_factory=list)
    #: Units systemd still knows about that device.conf no longer declares.
    #: A deleted library leaves these behind until the machine is rebooted.
    stale: List[str] = field(default_factory=list)

    @property
    def libraries_active(self) -> int:
        return sum(1 for unit in self.libraries if unit.active)

    @property
    def drives_active(self) -> int:
        return sum(1 for unit in self.drives if unit.active)

    @property
    def daemons_total(self) -> int:
        return len(self.libraries) + len(self.drives)

    @property
    def daemons_active(self) -> int:
        return self.libraries_active + self.drives_active

    @property
    def healthy(self) -> bool:
        """Everything configured is running, and the module is loaded."""
        return (self.target_active and self.modules_loaded
                and self.daemons_total > 0
                and self.daemons_active == self.daemons_total)

    def to_dict(self) -> Dict:
        return {
            'target_active': self.target_active,
            'target_enabled': self.target_enabled,
            'modules_loaded': self.modules_loaded,
            'healthy': self.healthy,
            'libraries': [unit.to_dict() for unit in self.libraries],
            'drives': [unit.to_dict() for unit in self.drives],
            'libraries_active': self.libraries_active,
            'libraries_total': len(self.libraries),
            'drives_active': self.drives_active,
            'drives_total': len(self.drives),
            'daemons_active': self.daemons_active,
            'daemons_total': self.daemons_total,
            'stale': self.stale,
        }


def _is(unit: str, question: str) -> str:
    """systemctl is-active / is-enabled, as a word."""
    result = shell.run(['systemctl', question, unit], timeout=QUICK)
    return result.stdout.strip() or result.stderr.strip()


def is_active(unit: str) -> bool:
    """Is this unit running right now?

    `systemctl is-active` exits non-zero for an inactive unit, so the exit code
    says nothing useful on its own; the word it prints is the answer.
    """
    return _is(unit, 'is-active') == 'active'


def is_enabled(unit: str) -> bool:
    """Will this unit start at boot?"""
    return _is(unit, 'is-enabled') == 'enabled'


def _instances(pattern: str, matcher) -> List[UnitState]:
    """Every loaded instance of a templated unit, running or not."""
    result = shell.run(
        ['systemctl', 'list-units', '--all', '--no-legend', '--plain', pattern],
        timeout=QUICK)
    if not result.ok:
        return []

    units = []
    for line in result.stdout.splitlines():
        match = matcher.match(line.strip())
        if not match:
            continue
        fields = line.split()
        active = 'active' in fields and 'inactive' not in fields
        state = next((f for f in fields if f in
                      ('active', 'inactive', 'failed', 'activating')), 'unknown')
        units.append(UnitState(name=match.group(1), active=active, state=state))
    return units


def status(config_dir=None) -> MhvtlServiceStatus:
    """The state of every MHVTL unit.

    Units are compared against device.conf so that instances left behind by a
    deleted library are named rather than counted as failures. systemd keeps a
    template instance loaded until the machine reboots, so a deleted library
    otherwise shows up for ever as "3 of 4 libraries running".
    """
    from . import modules

    state = MhvtlServiceStatus(
        target_active=_is(TARGET, 'is-active') == 'active',
        target_enabled=_is(TARGET, 'is-enabled') == 'enabled',
        modules_loaded=modules.mhvtl_loaded(),
        libraries=_instances('vtllibrary@*', LIBRARY_UNIT_RE),
        drives=_instances('vtltape@*', TAPE_UNIT_RE))

    declared = _declared_ids(config_dir)
    if declared is not None:
        libraries, drives = declared
        known = libraries | drives
        for unit in list(state.libraries) + list(state.drives):
            instance = unit.name.split('@', 1)[1].split('.', 1)[0]
            if instance.isdigit() and int(instance) not in known:
                state.stale.append(unit.name)
        state.libraries = [u for u in state.libraries if u.name not in state.stale]
        state.drives = [u for u in state.drives if u.name not in state.stale]

    return state


def _declared_ids(config_dir=None):
    """Library and drive ids from device.conf, or None if it cannot be read."""
    try:
        from ..config import device_conf as device_conf_format
        from ..core import device_conf_path

        result = shell.sudo_cat(device_conf_path(config_dir))
        if not result.ok:
            return None
        conf = device_conf_format.parse(result.stdout)
        return (set(conf.libraries), set(conf.drives))
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.warning('cannot read device.conf to check for stale units: %s', exc)
        return None


def _control(action: str, unit: str = TARGET):
    """start/stop/restart a unit. Needs the sudoers rule for systemctl."""
    return shell.sudo(['systemctl', action, unit], timeout=SLOW)


def start(unit: str = TARGET):
    return _control('start', unit)


def stop(unit: str = TARGET):
    return _control('stop', unit)


def restart(unit: str = TARGET):
    return _control('restart', unit)


def daemon_reload():
    """Needed after unit files change - adding a library writes new instances."""
    return shell.sudo(['systemctl', 'daemon-reload'], timeout=SLOW)


def enable(unit: str):
    """Make a unit start at boot. Creating a library writes new instances."""
    return _control('enable', unit)


def disable(unit: str):
    return _control('disable', unit)


def library_units(library_id: int, drive_ids) -> List[str]:
    """The unit names one library owns: its robot, then one per drive.

    Taking the drive ids from device.conf rather than assuming library_id+1..n:
    a library whose drives were added and removed over time has gaps, and
    guessing the range leaves the real units running after a delete.
    """
    return ([f'vtllibrary@{int(library_id)}.service'] +
            [f'vtltape@{int(drive_id)}.service' for drive_id in drive_ids])


def start_library(library_id: int, drive_ids) -> Dict[str, bool]:
    """Enable and start a library's units. Best effort: the configuration is
    already written, so a unit that will not start is reported, not fatal."""
    daemon_reload()
    outcome = {}
    for unit in library_units(library_id, drive_ids):
        enable(unit)
        outcome[unit] = start(unit).ok
    return outcome


def stop_library(library_id: int, drive_ids) -> Dict[str, bool]:
    """Stop and disable a library's units, before its configuration goes away.

    Stopping first matters: a daemon whose device.conf entry has vanished logs
    errors until it is killed, and its unit stays loaded until the next reboot.
    """
    outcome = {}
    for unit in library_units(library_id, drive_ids):
        outcome[unit] = stop(unit).ok
        disable(unit)
    daemon_reload()
    return outcome


def forget_library(library_id: int, drive_ids) -> None:
    """After a library has left device.conf: drop what systemd still holds of it.

    MHVTL 1.8 ships a systemd generator (mhvtl-device-conf-generator) that reads
    device.conf at every daemon-reload and links a vtllibrary@/vtltape@ unit
    into mhvtl.target.wants for each entry - "enabled-runtime". stop_library()
    reloads while the library is still in the file, so the generator put the
    links straight back and every removed library left its units enabled until
    the next reboot. Reloading once the entry is gone removes them.
    """
    daemon_reload()
    for unit in library_units(library_id, drive_ids):
        reset_failed(unit)


def reset_failed(unit: str):
    """Clear a failed unit's state.

    A template instance for a library that no longer exists stays in the failed
    list until this is called or the machine reboots, and a dashboard counting
    failed units reports a healthy host as broken for ever.
    """
    return shell.sudo(['systemctl', 'reset-failed', unit], timeout=QUICK)


def all_vtl_units() -> List[str]:
    """Every loaded vtllibrary@ and vtltape@ instance, running or not.

    Includes instances systemd still knows about after their device.conf entry
    has gone, which is exactly what looking for orphans needs.
    """
    result = shell.run(
        ['systemctl', 'list-units', '--all', '--no-legend', '--plain', 'vtl*'],
        timeout=QUICK)
    if not result.ok:
        return []

    names = []
    for line in result.stdout.splitlines():
        match = re.search(r'(vtllibrary@\d+\.service|vtltape@\d+\.service)', line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def instance_id(unit: str) -> Optional[int]:
    """The device.conf id in a template instance name, or None."""
    match = re.search(r'@(\d+)\.service$', unit)
    return int(match.group(1)) if match else None


def pids_of(unit: str) -> List[int]:
    """The pids systemd still associates with a unit.

    Read from systemctl rather than found by pattern: the version this replaces
    ran `pkill -9 -f "vtltape.*-q<id>"`, which matches on a command line and so
    matches anything else that happens to contain that text - including, during
    this refactor, the shell that ran it.
    """
    result = shell.run(['systemctl', 'show', unit,
                        '--property=MainPID,ControlPID,ExecMainPID'],
                       timeout=QUICK)
    pids = []
    for line in result.stdout.splitlines():
        _, _, value = line.partition('=')
        value = value.strip()
        if value.isdigit() and int(value) > 0 and int(value) not in pids:
            pids.append(int(value))
    return pids


def kill_lingering(unit: str) -> List[int]:
    """SIGKILL the processes a stopped unit left behind, and only those.

    A daemon whose device.conf entry has been removed does not always exit on
    `systemctl stop`, and it holds the message queue the next library with that
    id would use. Each pid is checked against /proc before it is signalled, so a
    pid that has already exited and been reused is not killed in its successor's
    place.
    """
    killed = []
    for pid in pids_of(unit):
        expected = instance_id(unit)
        try:
            cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().decode(
                'utf-8', 'replace').replace('\0', ' ')
        except OSError:
            continue                                   # already gone

        if 'vtltape' not in cmdline and 'vtllibrary' not in cmdline:
            logger.warning('not killing pid %s for %s: it is running %r',
                           pid, unit, cmdline.strip()[:80])
            continue
        if expected is not None and f'-q {expected}' not in cmdline \
                and f'-q{expected}' not in cmdline:
            logger.warning('not killing pid %s: it is an MHVTL daemon but not '
                           'queue %s (%r)', pid, expected, cmdline.strip()[:80])
            continue

        if shell.sudo(['kill', '-9', str(pid)], timeout=QUICK).ok:
            killed.append(pid)
    return killed


def restart_library(library_id: int = None) -> Dict[str, Any]:
    """Restart one library's robot, or - with no library - the whole target.

    A library that will not restart is reported, never escalated. This used to
    fall back to restarting the target, on the grounds that a new unit file is
    unknown until daemon-reload; but the daemon-reload runs first anyway, and
    vtllibrary@ is a template, so an instance needs no file of its own. What the
    fallback did in practice was restart every other library on the host - a
    job running on one lost its drive, and every iSCSI export was left bound to
    devices that no longer existed - because one of them had failed.
    """
    daemon_reload()
    if library_id is None:
        result = restart(TARGET)
        return {'restarted': TARGET, 'ok': result.ok,
                'error': result.stderr.strip() or None}

    unit = f'vtllibrary@{int(library_id)}.service'
    result = restart(unit)
    return {'restarted': unit, 'ok': result.ok,
            'error': None if result.ok else (result.stderr.strip() or
                                             'systemctl reported a failure')}
