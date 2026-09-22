"""Keeping live pscsi backstores bound to the devices they export.

remap.py fixes the saved configuration before boot. This module fixes the
running one, which breaks in a way nothing reports.

A pscsi backstore binds to a kernel scsi_device when it is enabled, not to a
path. Every MHVTL device belongs to one daemon - the changer to vtllibrary@L,
each drive to vtltape@D - and the daemon removes its device when it stops and
adds a new one when it starts. Restart the daemon and the backstore keeps the
old, deleted device: the target still logs initiators in, and reports no LUNs.
Nothing is logged on either side. Library 10's export went that way when the
Remove Library page restarted mhvtl.target, and stayed that way for a day.

Detecting it. A device keeps its SCSI address across a restart, and often its
/dev/sgN node as well, so nothing in configfs changes. What does change is the
daemon's systemd InvocationID, so each binding is recorded with the
InvocationID of the unit that owned the device at the time. A binding whose
unit has been started since is stale. Two things are also stale without a
record: the node the backstore was created on now belongs to another device,
or nothing is at its address at all.

Repairing it - rebind(). The backstore is deleted and created again on the
device now at its address, then every LUN that used it is recreated at the same
index, and every ACL's mapped LUN with the same number and write protection.
Targets, portals, ACLs and their sessions are never touched: an initiator sees
its LUNs come back rather than a target that went away.

Which library a backstore belongs to comes from the SCSI address the kernel
reports for it and device.conf, not from its name: the Export Library form
names backstores after the vendor and model, the CLI lib<L>_changer.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config.service import ConfigService
from ..core import ServiceResult, failure_result, shell, success_result
from ..core.paths import _setting, config_dir
from ..scsi import lsscsi
from . import targetcli

logger = logging.getLogger(__name__)

CONFIGFS = '/sys/kernel/config/target/core'
DEFAULT_STATE = '/var/lib/mhvtl-gui/iscsi-bindings.json'

#: `/sys/.../pscsi_2/lib10_changer/info:  SCSI Device Bus Location: Channel ID: 0 Target ID: 1 LUN: 0 Host ID: 16`
INFO_RE = re.compile(r'/pscsi_\d+/(?P<name>[^/]+)/info:.*Channel ID:\s*(?P<channel>\d+)'
                     r'\s+Target ID:\s*(?P<target>\d+)\s+LUN:\s*(?P<lun>\d+)')

BOUND, STALE, MISSING, UNKNOWN = 'bound', 'stale', 'missing', 'unknown'
#: Taken out of the configuration at boot because its device was not there;
#: see remap.py. Put back by restore_withheld() once the device exists.
WITHHELD = 'withheld'
DEFAULT_WITHHELD = '/var/lib/mhvtl-gui/iscsi-withheld.json'

Address = Tuple[int, int, int]


def state_path() -> str:
    return _setting('MHVTL_ISCSI_BINDINGS', DEFAULT_STATE)


class RebindDisabled(RuntimeError):
    """MHVTL_ISCSI_REBIND is off - the test settings turn it off."""


def rebind_allowed() -> bool:
    """False under the test settings, so no test can reach the live exports.

    A test that mocked the daemon restart but not this module once rebound
    library 10's real backstores. Read directly rather than through _setting,
    which turns False into the default.
    """
    try:
        from django.conf import settings
        return bool(getattr(settings, 'MHVTL_ISCSI_REBIND', True))
    except Exception:                                  # noqa: BLE001 - no Django
        return True


def _require_allowed() -> None:
    if not rebind_allowed():
        raise RebindDisabled('MHVTL_ISCSI_REBIND is off; the live iSCSI '
                             'configuration was not changed')


# -- reading ---------------------------------------------------------------

@dataclass
class Binding:
    """One pscsi backstore and what is known about the device under it."""
    name: str
    dev: Optional[str]                 # the node it was created on
    address: Optional[Address]         # where the kernel says its device is
    library_id: Optional[int] = None
    unit: Optional[str] = None         # the daemon that owns that device
    current: Optional[str] = None      # the node at that address now
    status: str = UNKNOWN
    reason: str = ''
    luns: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {'name': self.name, 'dev': self.dev,
                'address': list(self.address) if self.address else None,
                'library_id': self.library_id, 'unit': self.unit,
                'current': self.current, 'status': self.status,
                'reason': self.reason, 'luns': self.luns}


def running_config() -> Optional[Dict]:
    """The live LIO configuration, as saveconfig.json would describe it.

    Saved to a temporary file of its own. targetcli rotates backups only for the
    default file, so /etc/target is not touched. Only commands the packaged
    sudoers rules already allow: targetcli, cat, rm.
    """
    path = f'/tmp/mhvtl-lio-{uuid.uuid4().hex}.json'
    try:
        # Both steps say why they failed. They used to return None in silence,
        # and the pages then said only that the configuration could not be
        # read - which is how a targetcli that could not create its own
        # ~/.targetcli under the service's sandbox went undiagnosed.
        saved = targetcli.run(['saveconfig', f'savefile={path}'])
        if not saved.ok:
            logger.warning('targetcli saveconfig failed (exit %s%s): %s',
                           saved.returncode, ', timed out' if saved.timed_out else '',
                           (saved.stderr or saved.stdout or '').strip()[:500])
            return None
        read = shell.sudo(['cat', path])
        if not read.ok:
            logger.warning('reading %s back failed (exit %s): %s', path,
                           read.returncode, (read.stderr or '').strip()[:300])
            return None
        return json.loads(read.stdout)
    except json.JSONDecodeError as exc:
        logger.warning('reading the running LIO configuration: %s', exc)
        return None
    finally:
        shell.sudo(['rm', '-f', path, f'{path}.temp'])


def bound_addresses() -> Dict[str, Address]:
    """Backstore name -> (channel, target, lun) of the device it holds.

    From configfs, which describes the device the backstore bound to even after
    that device has been deleted - which is the point. configfs is readable
    without privileges.
    """
    lines = []
    for info in Path(CONFIGFS).glob('pscsi_*/*/info'):
        try:
            text = info.read_text(errors='replace')
        except OSError as exc:
            logger.warning('reading %s: %s', info, exc)
            continue
        lines.extend(f'{info}:{line}' for line in text.splitlines()
                     if 'Bus Location' in line)
    return parse_info('\n'.join(lines))


def parse_info(text: str) -> Dict[str, Address]:
    addresses = {}
    for line in text.splitlines():
        match = INFO_RE.search(line)
        if match:
            addresses[match['name']] = (int(match['channel']), int(match['target']),
                                        int(match['lun']))
    return addresses


def owners(conf) -> Dict[Address, Tuple[int, str]]:
    """(channel, target, lun) -> (library id, the unit that owns the device)."""
    owned = {}
    for library_id in conf.libraries:
        address = conf.address_of(library_id)
        if address:
            owned[address] = (int(library_id), f'vtllibrary@{int(library_id)}.service')
        for drive_id in conf.drives_of(library_id):
            address = conf.address_of(drive_id)
            if address:
                owned[address] = (int(library_id), f'vtltape@{int(drive_id)}.service')
    return owned


def invocation_ids(unit_names) -> Dict[str, str]:
    """unit -> its current InvocationID; empty for a unit that is not running."""
    ids = {}
    for unit in sorted(set(unit_names)):
        shown = shell.run(['systemctl', 'show', '-p', 'InvocationID', '--value', unit])
        ids[unit] = shown.stdout.strip() if shown.ok else ''
    return ids


def load_state() -> Dict[str, Dict]:
    read = shell.sudo_cat(state_path())
    if not read.ok:
        return {}
    try:
        state = json.loads(read.stdout)
    except json.JSONDecodeError:
        logger.warning('%s is not valid JSON; treating every binding as unknown',
                       state_path())
        return {}
    return state if isinstance(state, dict) else {}


def withheld_path() -> str:
    return _setting('MHVTL_ISCSI_WITHHELD', DEFAULT_WITHHELD)


def load_withheld() -> Dict[str, Dict]:
    read = shell.sudo_cat(withheld_path())
    if not read.ok:
        return {}
    try:
        data = json.loads(read.stdout)
    except json.JSONDecodeError:
        logger.warning('%s is not valid JSON; ignoring it', withheld_path())
        return {}
    return data if isinstance(data, dict) else {}


def save_withheld(entries: Dict[str, Dict]) -> bool:
    written = shell.sudo_tee(withheld_path(), json.dumps(entries, indent=2, sort_keys=True) + '\n')
    if not written.ok:
        logger.warning('could not write %s: %s', withheld_path(), written.output.strip()[:200])
    return written.ok


def add_withheld(entries: Dict[str, Dict]) -> bool:
    """Remember backstores remap took out at boot. Earlier entries are kept."""
    current = load_withheld()
    current.update(entries)
    return save_withheld(current)


def restore_withheld(library_id: int = None, *, config_directory=None) -> List[Dict]:
    """Put back the withheld backstores whose device now exists.

    Each one is created on the device now at its SCSI address - never on the
    /dev/sgN it had before the reboot - with its LUNs at the same numbers and
    its ACL mappings. One whose device is still missing stays withheld.
    Returns what was done, for the caller's report.
    """
    entries = load_withheld()
    if not entries:
        return []
    conf = ConfigService(config_directory).device_conf()
    owned = owners(conf) if conf is not None else {}
    by_address = {d.address.ctl: d for d in lsscsi.local() if d.address and d.generic_path}
    done = []
    for name, entry in sorted(entries.items()):
        address = tuple(entry.get('address') or ())
        owner = owned.get(address)
        if library_id is not None and (owner is None or owner[0] != int(library_id)):
            continue
        device = by_address.get(address)
        if device is None:
            done.append({'name': name, 'restored': False,
                         'why': f'nothing at {_ctl(address)} yet'})
            continue
        binding = Binding(name=name, dev=None, address=address,
                          library_id=owner[0] if owner else None,
                          unit=owner[1] if owner else None,
                          current=device.generic_path, status=WITHHELD,
                          reason='withheld at boot', luns=entry.get('luns') or [])
        errors = _rebind_one(binding, delete=False)
        if errors:
            done.append({'name': name, 'restored': False, 'why': '; '.join(errors)})
            continue
        del entries[name]
        done.append({'name': name, 'restored': True, 'now': device.generic_path,
                     'luns': len(binding.luns)})
    save_withheld(entries)
    if any(item['restored'] for item in done):
        targetcli.save_config()
        record([item['name'] for item in done if item['restored']],
               config_directory=config_directory)
    return done


def save_state(state: Dict[str, Dict]) -> bool:
    path = state_path()
    written = shell.sudo_tee(path, json.dumps(state, indent=2, sort_keys=True) + '\n')
    if not written.ok:
        logger.warning('could not write %s: %s', path, written.output.strip()[:200])
    return written.ok


# -- deciding --------------------------------------------------------------

def assess(config: Dict, addresses: Dict[str, Address], conf, devices,
           invocations: Dict[str, str], state: Dict[str, Dict]) -> List[Binding]:
    """Every pscsi backstore, whose device it holds and whether that still holds.

    Pure: the inputs are what running_config, bound_addresses, device.conf,
    lsscsi, systemctl and the state file said.
    """
    by_address = {device.address.ctl: device for device in devices
                  if device.address and device.generic_path}
    owned = owners(conf)
    luns = lun_users(config)

    bindings = []
    for obj in config.get('storage_objects') or []:
        if obj.get('plugin') != 'pscsi':
            continue
        name = obj.get('name', '')
        binding = Binding(name=name, dev=obj.get('dev'), address=addresses.get(name),
                          luns=luns.get(name, []))
        if binding.address in owned:
            binding.library_id, binding.unit = owned[binding.address]
        device = by_address.get(binding.address)
        binding.current = device.generic_path if device else None
        binding.status, binding.reason = _judge(binding, invocations, state.get(name))
        bindings.append(binding)
    return bindings


def _judge(binding: Binding, invocations: Dict[str, str],
           record: Optional[Dict]) -> Tuple[str, str]:
    if binding.address is None:
        return UNKNOWN, 'the kernel reports no device address for it'
    if binding.unit is None:
        return UNKNOWN, 'no device in device.conf has its address; left alone'
    if binding.current is None:
        return MISSING, f'nothing is at {_ctl(binding.address)} - {binding.unit} is not running'
    if binding.dev != binding.current:
        return STALE, (f'created on {binding.dev}, but the device at '
                       f'{_ctl(binding.address)} is now {binding.current}')
    now = invocations.get(binding.unit, '')
    if not record:
        return UNKNOWN, f'no record of which start of {binding.unit} it bound to'
    if record.get('unit') != binding.unit or record.get('invocation') != now:
        return STALE, f'{binding.unit} has restarted since it was bound'
    return BOUND, f'bound to the running {binding.unit}'


def _ctl(address: Address) -> str:
    return ':'.join(str(part) for part in address)


def lun_users(config: Dict) -> Dict[str, List[Dict]]:
    """Backstore name -> every LUN exporting it, with the ACL mappings of each."""
    users: Dict[str, List[Dict]] = {}
    for target in config.get('targets') or []:
        for tpg in target.get('tpgs') or []:
            for lun in tpg.get('luns') or []:
                plugin_name = (lun.get('storage_object') or '').split('/')[-2:]
                if len(plugin_name) != 2 or plugin_name[0] != 'pscsi':
                    continue
                mapped = [{'node_wwn': acl.get('node_wwn'), 'index': m.get('index'),
                           'write_protect': bool(m.get('write_protect'))}
                          for acl in tpg.get('node_acls') or []
                          for m in acl.get('mapped_luns') or []
                          if m.get('tpg_lun') == lun.get('index')]
                users.setdefault(plugin_name[1], []).append({
                    'fabric': target.get('fabric'), 'wwn': target.get('wwn'),
                    'tpg': tpg.get('tag', 1), 'index': lun.get('index'),
                    'mapped': mapped})
    return users


# -- the operations --------------------------------------------------------

def _gather(config_directory=None):
    config = running_config()
    if config is None:
        return None, 'the running LIO configuration cannot be read (is targetcli installed?)'
    conf = ConfigService(config_directory).device_conf()
    if conf is None:
        return None, 'device.conf cannot be read'
    devices = lsscsi.local()
    owned = owners(conf)
    state = load_state()
    bindings = assess(config, bound_addresses(), conf, devices,
                      invocation_ids(unit for _, unit in owned.values()), state)
    return (config, conf, bindings, state), ''


def status(library_id: int = None, *, config_directory=None) -> ServiceResult:
    """Which exported devices still answer, library by library."""
    operation_id = str(uuid.uuid4())[:8]
    gathered, problem = _gather(config_directory)
    if gathered is None:
        return failure_result('Cannot check the iSCSI bindings', [problem], operation_id)
    bindings = [b for b in gathered[2]
                if library_id is None or b.library_id == int(library_id)]
    # The ones remap took out at boot are not in the running configuration at
    # all; they are shown from the withheld file, so they are not forgotten.
    owned = owners(gathered[1])
    for name, entry in sorted(load_withheld().items()):
        address = tuple(entry.get('address') or ())
        owner = owned.get(address)
        if library_id is not None and (owner is None or owner[0] != int(library_id)):
            continue
        bindings.append(Binding(
            name=name, dev=entry.get('dev'), address=address,
            library_id=owner[0] if owner else None, unit=owner[1] if owner else None,
            status=WITHHELD, luns=entry.get('luns') or [],
            reason=f'withheld at boot {entry.get("withheld", "")}: nothing was at '
                   f'{_ctl(address)}; Re-bind puts it back once the device is there'))
    stale = [b.name for b in bindings if b.status in (STALE, MISSING, WITHHELD)]
    message = (f'{len(stale)} exported device(s) no longer answer: {", ".join(stale)}'
               if stale else 'Every exported device is bound to a running daemon')
    return success_result(message, {'bindings': [b.to_dict() for b in bindings],
                                    'stale': stale}, operation_id)


def forget(names) -> bool:
    """Drop the record of these backstores, for when they no longer exist.

    Called when a library is unexported: a binding kept for a backstore that
    has been deleted makes `iscsi remap` try to repoint something that is not
    there, and makes the iSCSI page list a device nothing can reach.

    Returns whether the record was written. A record that cannot be written
    is not worth failing an unexport over - the backstore is already gone.
    """
    wanted = set(names or ())
    if not wanted:
        return True

    state = load_state()
    kept = {name: entry for name, entry in state.items() if name not in wanted}
    if len(kept) == len(state):
        return True                       # nothing of theirs was recorded
    return save_state(kept)


def record(names=None, *, config_directory=None) -> ServiceResult:
    """Remember which start of each owning daemon the named backstores bound to.

    Called once the bindings are known to be right: after an export, after
    rebind(), and at boot once remap has pointed the saved ones correctly.
    With no names, records every backstore whose node matches its address.
    """
    operation_id = str(uuid.uuid4())[:8]
    _require_allowed()
    gathered, problem = _gather(config_directory)
    if gathered is None:
        return failure_result('Cannot record the iSCSI bindings', [problem], operation_id)
    _, _, bindings, state = gathered
    wanted = set(names) if names is not None else None
    ids = invocation_ids(b.unit for b in bindings if b.unit)
    recorded = []
    for binding in bindings:
        if wanted is not None and binding.name not in wanted:
            continue
        if not binding.unit or binding.current is None or binding.current != binding.dev:
            continue
        state[binding.name] = {'unit': binding.unit, 'invocation': ids.get(binding.unit, ''),
                               'address': list(binding.address), 'dev': binding.dev,
                               'recorded': datetime.now().isoformat(timespec='seconds')}
        recorded.append(binding.name)
    known = {b.name for b in bindings}
    for name in [name for name in state if name not in known]:
        del state[name]                    # the backstore was deleted
    if not save_state(state):
        return failure_result(f'Could not write {state_path()}', [], operation_id)
    return success_result(f'Recorded {len(recorded)} binding(s)',
                          {'recorded': recorded}, operation_id)


def record_saved(saved: Dict, conf, devices) -> Dict[str, Dict]:
    """Records for a saved configuration that target.service is about to restore.

    At boot nothing records anything: remap points the saved nodes at the right
    devices, then target.service binds them, and every export would show as
    stale against the daemons it is in fact bound to. remap calls this just
    before target.service runs, when the daemons it names are the ones the
    restore will bind to. Keyed on the node, not the name, like everything
    here: the device at that node now, its address, and the daemon that owns it.
    Returns the updated state; the caller writes it.
    """
    by_node = {device.generic_path: device for device in devices
               if device.address and device.generic_path}
    owned = owners(conf)
    found = {}
    for obj in saved.get('storage_objects') or []:
        device = by_node.get(obj.get('dev'))
        if obj.get('plugin') != 'pscsi' or device is None:
            continue
        owner = owned.get(device.address.ctl)
        if owner:
            found[obj['name']] = (owner[1], device.address.ctl, obj['dev'])
    ids = invocation_ids(unit for unit, _, _ in found.values())
    stamp = datetime.now().isoformat(timespec='seconds')
    state = {name: {'unit': unit, 'invocation': ids.get(unit, ''),
                    'address': list(address), 'dev': dev, 'recorded': stamp}
             for name, (unit, address, dev) in found.items()}
    return state


def rebind(library_id: int, *, names=None, wait: float = 30,
           config_directory=None) -> ServiceResult:
    """Bind a library's exported devices to the daemons running now.

    Rebinds the backstores that are stale or of unknown freshness; one still
    bound to its running daemon is left alone. A backstore whose device is not
    there is reported and left alone - the daemon has to be running first.
    wait gives a daemon that has just been restarted time to add its device.
    """
    operation_id = str(uuid.uuid4())[:8]
    _require_allowed()
    library_id = int(library_id)
    deadline = time.monotonic() + max(wait, 0)
    while True:
        gathered, problem = _gather(config_directory)
        if gathered is None:
            return failure_result(f'Cannot rebind library {library_id}', [problem],
                                  operation_id)
        config, _, bindings, _ = gathered
        mine = [b for b in bindings if b.library_id == library_id
                and (names is None or b.name in names)]
        if not any(b.status == MISSING for b in mine) or time.monotonic() >= deadline:
            break
        time.sleep(2)

    # Withheld at boot and back now: put them back before anything else.
    restored = restore_withheld(library_id, config_directory=config_directory)
    back = [item for item in restored if item['restored']]

    todo = [b for b in mine if b.status in (STALE, UNKNOWN)]
    report = {'library_id': library_id, 'rebound': [], 'left': [], 'failed': [],
              'restored': back}
    for binding in mine:
        if binding.status in (BOUND, MISSING):
            report['left'].append({'name': binding.name, 'why': binding.reason})
    for item in restored:
        if not item['restored']:
            report['left'].append({'name': item['name'], 'why': item['why']})
    put_back = (f'; put back {len(back)} withheld at boot: '
                f'{", ".join(item["name"] for item in back)}') if back else ''
    if not mine and not back:
        return success_result(f'Library {library_id} has no exported devices',
                              report, operation_id)
    if not todo:
        return success_result(f'Library {library_id}: nothing to rebind{put_back}',
                              report, operation_id)

    for binding in todo:
        errors = _rebind_one(binding)
        if errors:
            report['failed'].append({'name': binding.name, 'errors': errors})
        else:
            report['rebound'].append({'name': binding.name, 'was': binding.dev,
                                      'now': binding.current, 'why': binding.reason,
                                      'luns': len(binding.luns)})

    saved = targetcli.save_config()
    if not saved.ok:
        report['failed'].append({'name': 'saveconfig',
                                 'errors': [saved.output.strip()[:300]]})
    if report['rebound']:
        record([item['name'] for item in report['rebound']],
               config_directory=config_directory)

    if report['failed']:
        return failure_result(
            f'Library {library_id}: {len(report["failed"])} backstore(s) could not be rebound',
            [f'{item["name"]}: {"; ".join(item["errors"])}' for item in report['failed']],
            operation_id)
    return success_result(f'Library {library_id}: rebound {len(report["rebound"])} '
                          f'exported device(s){put_back}', report, operation_id)


def _rebind_one(binding: Binding, delete: bool = True) -> List[str]:
    """Delete the backstore, create it on the current device, restore its LUNs.

    delete=False for one that is not there to delete - a withheld one."""
    foreign = [lun for lun in binding.luns if lun['fabric'] != 'iscsi']
    if foreign:
        return [f'it is also exported over {foreign[0]["fabric"]}, which this does '
                f'not know how to restore; rebind it by hand']
    try:
        targetcli.validate_device(binding.current)
    except targetcli.DeviceRefused as exc:
        return [str(exc)]

    logger.info('iscsi rebind %s: %s -> %s (%s)', binding.name, binding.dev,
                binding.current, binding.reason)
    steps = ([['/backstores/pscsi', 'delete', binding.name]] if delete else []) + \
            [['/backstores/pscsi', 'create', f'name={binding.name}',
              f'dev={binding.current}']]
    for lun in binding.luns:
        tpg = f'/iscsi/{lun["wwn"]}/tpg{int(lun["tpg"])}'
        steps.append([f'{tpg}/luns', 'create', f'/backstores/pscsi/{binding.name}',
                      f'lun={int(lun["index"])}', 'add_mapped_luns=false'])
        for mapped in lun['mapped']:
            steps.append([f'{tpg}/acls/{mapped["node_wwn"]}', 'create',
                          f'mapped_lun={int(mapped["index"])}',
                          f'tpg_lun_or_backstore={int(lun["index"])}',
                          f'write_protect={int(mapped["write_protect"])}'])

    for step in steps:
        result = targetcli.run(step)
        if not result.ok:
            return [f'`targetcli {" ".join(step)}` failed: '
                    f'{result.output.strip()[:300]}']
    return []


def after_restart(library_id: int = None, *, config_directory=None) -> str:
    """Rebind what a daemon restart left exported to deleted devices.

    Called by every code path that restarts MHVTL daemons. None means the whole
    target was restarted, so every library with exports is checked. Returns a
    note for the caller's message - empty when nothing is exported - and never
    raises: the restart already happened, and failing it now would only hide
    what was done.
    """
    if not rebind_allowed():
        return ''
    live = Path(config_dir()).resolve()
    if config_directory is not None and Path(config_directory).resolve() != live:
        # Exports belong to the daemons that read the live configuration; a
        # restart made on behalf of another directory is not theirs.
        return ''
    try:
        if library_id is not None:
            libraries = [int(library_id)]
        else:
            checked = status(config_directory=config_directory)
            if not checked.success:
                return ''
            libraries = sorted({b['library_id'] for b in checked.data['bindings']
                                if b['library_id'] is not None})
        notes = []
        for library in libraries:
            result = rebind(library, config_directory=config_directory)
            if not result.success:
                notes.append(result.message)
            elif result.data.get('rebound') or result.data.get('restored'):
                notes.append(result.message)
        return '; '.join(notes)
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('rebinding iSCSI exports after a restart')
        return f'the iSCSI exports were not checked ({exc})'


def release_library(library_id: int, *, config_directory=None) -> ServiceResult:
    """Delete every pscsi backstore bound to one of a library's devices.

    Called before a library is removed. A backstore holds a reference to its
    kernel device, and a device removed while one is held is never freed: it
    stays on the SCSI host's list at its address, every later device there is
    refused by the target with "scsi_device_get() failed", and the mhvtl module
    can no longer be unloaded. Only a reboot clears it. Deleting the backstore
    first drops the LUNs that use it and releases the reference.
    """
    operation_id = str(uuid.uuid4())[:8]
    library_id = int(library_id)
    if not rebind_allowed():
        return success_result('iSCSI exports not checked (MHVTL_ISCSI_REBIND off)',
                              {'released': []}, operation_id)
    live = Path(config_dir()).resolve()
    if config_directory is not None and Path(config_directory).resolve() != live:
        return success_result('Not the live configuration; exports not checked',
                              {'released': []}, operation_id)
    gathered, problem = _gather(config_directory)
    if gathered is None:
        # No targetcli, or it cannot be read: nothing can be holding anything.
        return success_result(f'iSCSI exports not checked: {problem}',
                              {'released': []}, operation_id)
    _, _, found, state = gathered
    mine = [b for b in found if b.library_id == library_id]
    released, errors = [], []
    for binding in mine:
        result = targetcli.delete_backstore('pscsi', binding.name)
        if result.ok:
            released.append(binding.name)
            state.pop(binding.name, None)
        else:
            errors.append(f'{binding.name}: {result.output.strip()[:200]}')
    if released:
        targetcli.save_config()
        save_state(state)
        logger.info('released the iSCSI exports of library %s: %s', library_id, released)
    if errors:
        return failure_result(f'Could not release the iSCSI exports of library {library_id}',
                              errors, operation_id)
    message = (f'Released {len(released)} iSCSI backstore(s): {", ".join(released)}'
               if released else 'No iSCSI exports to release')
    return success_result(message, {'released': released}, operation_id)
