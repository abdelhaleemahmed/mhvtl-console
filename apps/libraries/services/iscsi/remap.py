"""Keeping saved pscsi backstores pointed at the right devices across reboots.

A pscsi backstore is saved in /etc/target/saveconfig.json by device node -
/dev/sg5 - and target.service restores it by that node at boot. SCSI generic
numbers are assigned in probe order, which changes when the machine reboots or
the modules reload. After the reboot during step 9 the library10 target's LUN 0,
saved as "lib10_changer", came back as library 20's robot, on a target open to
any initiator.

This module rewrites the saved nodes before target.service reads them. It runs
from mhvtl-iscsi-remap.service, ordered After=mhvtl.target and
Before=target.service, so the wrong mapping is never live at all.

The backstore name is the mapping, so no extra state is kept:

    lib<L>_changer    the changer of library L
    lib<L>_drive<N>   library L's Nth drive, counting from 0 in id order

That is the convention iscsi/workflow.export_library and `mhvtl iscsi export`
use. A backstore whose name does not follow it is left alone and reported: this
module does not guess what an operator meant by a name it did not create.

A device that is expected but not visible - its daemon has not started - is
withheld: its backstore, the LUNs that use it and the ACL mappings to those
LUNs are taken out of the configuration target.service restores, and recorded
in the withheld file (bindings.WITHHELD) so they can be put back once the
device exists - by the next restart of that library, or Re-bind. Leaving it in
would have target.service open its old /dev/sgN, which after a reboot can be
another library's device. Exporting nothing is recoverable; exporting the
wrong robot is how tapes get overwritten.

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
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from ..config import device_conf as device_conf_format
from ..config.service import ConfigService
from ..core import ServiceResult, failure_result, shell, success_result
from ..scsi import lsscsi
from ..scsi.models import ScsiDevice

logger = logging.getLogger(__name__)

SAVECONFIG = '/etc/target/saveconfig.json'

#: lib10_changer, lib10_drive0 - the names export_library gives backstores.
NAME_RE = re.compile(r'^lib(?P<library>\d+)_(?:(?P<changer>changer)|drive(?P<drive>\d+))$')


@dataclass
class Change:
    """One backstore, where it points, and where it should."""
    name: str
    saved: Optional[str]
    wanted: Optional[str]
    reason: str
    withhold: bool = False             # device expected but not there: take it out
    address: Optional[tuple] = None

    @property
    def changes(self) -> bool:
        return self.withhold or (self.wanted is not None and self.wanted != self.saved)

    def to_dict(self) -> Dict:
        return {'name': self.name, 'saved': self.saved, 'wanted': self.wanted,
                'changes': self.changes, 'reason': self.reason,
                'withhold': self.withhold}


def expected_addresses(conf) -> Dict[str, tuple]:
    """Backstore name -> the (channel, target, lun) device.conf gives it."""
    wanted = {}
    for library_id, library in conf.libraries.items():
        address = conf.address_of(library_id)
        if address:
            wanted[f'lib{library_id}_changer'] = address
        for position, drive_id in enumerate(sorted(conf.drives_of(library_id))):
            address = conf.address_of(drive_id)
            if address:
                wanted[f'lib{library_id}_drive{position}'] = address
    return wanted


def plan(saved: Dict, conf, devices: List[ScsiDevice]) -> List[Change]:
    """What each saved pscsi backstore should point at now.

    Pure: takes the parsed saveconfig, the parsed device.conf and the devices
    the kernel reports, and says what would change.
    """
    by_address = {device.address.ctl: device for device in devices
                  if device.address and device.generic_path}
    addresses = expected_addresses(conf)

    changes = []
    for obj in saved.get('storage_objects') or []:
        if obj.get('plugin') != 'pscsi':
            continue
        name, current = obj.get('name', ''), obj.get('dev')

        if not NAME_RE.match(name):
            changes.append(Change(name, current, None,
                                  'not a name this tool created; left alone'))
            continue
        address = addresses.get(name)
        if address is None:
            changes.append(Change(name, current, None,
                                  'device.conf declares no such device; left alone'))
            continue
        device = by_address.get(address)
        if device is None:
            changes.append(Change(name, current, None,
                                  f'nothing at {address}; withheld until it is there',
                                  withhold=True, address=address))
            continue

        reason = ('already correct' if device.generic_path == current
                  else f'{current} is now {_describe(by_address, current)}')
        changes.append(Change(name, current, device.generic_path, reason))
    return changes


def _describe(by_address, node) -> str:
    """What a node is now, for the log line an operator will read."""
    for device in by_address.values():
        if device.generic_path == node:
            return f'{device.vendor} {device.model} at {device.address}'
    return 'not a library device'


def apply(saved: Dict, changes: List[Change]) -> Dict:
    """A copy of the saved configuration with the planned nodes written in,
    and the withheld backstores taken out with their LUNs and ACL mappings."""
    updated = json.loads(json.dumps(saved))
    wanted = {change.name: change.wanted for change in changes
              if change.changes and not change.withhold}
    withheld = {change.name for change in changes if change.withhold}
    for obj in updated.get('storage_objects') or []:
        if obj.get('plugin') == 'pscsi' and obj.get('name') in wanted:
            obj['dev'] = wanted[obj['name']]
    updated['storage_objects'] = [
        obj for obj in updated.get('storage_objects') or []
        if not (obj.get('plugin') == 'pscsi' and obj.get('name') in withheld)]
    for target in updated.get('targets') or []:
        for tpg in target.get('tpgs') or []:
            gone = {lun.get('index') for lun in tpg.get('luns') or []
                    if lun.get('storage_object') in
                    {f'/backstores/pscsi/{name}' for name in withheld}}
            tpg['luns'] = [lun for lun in tpg.get('luns') or [] if lun.get('index') not in gone]
            for acl in tpg.get('node_acls') or []:
                acl['mapped_luns'] = [m for m in acl.get('mapped_luns') or []
                                      if m.get('tpg_lun') not in gone]
    return updated


def withheld_entries(saved: Dict, changes: List[Change]) -> Dict[str, Dict]:
    """What it takes to put each withheld backstore back: where its device
    lives and every LUN and ACL mapping that used it."""
    from . import bindings
    users = bindings.lun_users(saved)
    stamp = datetime.now().isoformat(timespec='seconds')
    return {change.name: {'address': list(change.address), 'dev': change.saved,
                          'luns': users.get(change.name, []), 'withheld': stamp,
                          'reason': change.reason}
            for change in changes if change.withhold}


def wait_for_devices(conf, timeout: float, interval: float = 2.0) -> List[ScsiDevice]:
    """Discover devices, waiting until every declared address is visible.

    The 1.8 daemons add their units after mhvtl.target reports started, so the
    first look can be incomplete. Returns whatever is visible at the deadline;
    plan() leaves anything still missing alone.
    """
    expected = set(expected_addresses(conf).values())
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        devices = lsscsi.local()
        seen = {device.address.ctl for device in devices if device.address}
        if expected <= seen or time.monotonic() >= deadline:
            missing = expected - seen
            if missing:
                logger.warning('still not visible after waiting: %s', sorted(missing))
            return devices
        time.sleep(interval)


def _record_for_restore(saved: Dict, conf, devices) -> None:
    """Record which daemon each saved backstore is about to be bound to.

    target.service restores right after this, so without it every export
    would look stale on the iSCSI dashboard after a reboot. Best effort: a
    failure here must not stop target.service starting.
    """
    from . import bindings
    try:
        if bindings.rebind_allowed():
            bindings.save_state(bindings.record_saved(saved, conf, devices))
    except Exception as exc:                           # noqa: BLE001 - logged
        logger.warning('recording the iSCSI bindings for the restore: %s', exc)


def remap(*, dry_run: bool = False, wait: float = 0, config_directory=None,
          saveconfig: str = SAVECONFIG) -> ServiceResult:
    """Point every saved pscsi backstore at the device its name means.

    Backs up saveconfig.json beside itself before writing, and writes only when
    something changes. Does not touch the running target: at boot this runs
    before target.service, and by hand the caller decides when to restore.
    """
    operation_id = str(uuid.uuid4())[:8]

    read = shell.sudo_cat(saveconfig)
    if not read.ok:
        return success_result('No saved iSCSI configuration; nothing to remap',
                              {'changes': [], 'written': False}, operation_id)
    try:
        saved = json.loads(read.stdout)
    except json.JSONDecodeError as exc:
        return failure_result(f'{saveconfig} is not valid JSON', [str(exc)],
                              operation_id)

    conf = ConfigService(config_directory).device_conf()
    if conf is None:
        return failure_result('device.conf cannot be read',
                              ['without it no backstore can be resolved'],
                              operation_id)

    devices = wait_for_devices(conf, wait) if wait else lsscsi.local()
    changes = plan(saved, conf, devices)
    pending = [change for change in changes if change.changes]
    data = {'changes': [change.to_dict() for change in changes],
            'written': False, 'dry_run': dry_run}

    for change in changes:
        logger.info('iscsi remap %s: %s -> %s (%s)', change.name, change.saved,
                    change.wanted, change.reason)

    if not pending:
        if not dry_run:
            _record_for_restore(saved, conf, devices)
        return success_result('Every saved backstore already points at the right '
                              'device', data, operation_id)
    if dry_run:
        return success_result(f'{len(pending)} backstore(s) would be repointed',
                              data, operation_id)

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = shell.sudo(['cp', '-p', saveconfig, f'{saveconfig}.{stamp}.bak'])
    if not backup.ok:
        return failure_result('Refusing to rewrite saveconfig.json: the backup failed',
                              [backup.output.strip()[:300]], operation_id)

    written = shell.sudo_tee(saveconfig,
                             json.dumps(apply(saved, changes), indent=2) + '\n')
    if not written.ok:
        return failure_result(f'Could not write {saveconfig}',
                              [written.output.strip()[:300]], operation_id)

    data.update(written=True, backup=f'{saveconfig}.{stamp}.bak')
    _record_for_restore(apply(saved, changes), conf, devices)
    withheld = withheld_entries(saved, changes)
    if withheld:
        from . import bindings
        if not bindings.add_withheld(withheld):
            logger.warning('could not record the withheld backstores %s', sorted(withheld))
        data['withheld'] = sorted(withheld)
    moved = len(pending) - len(withheld)
    return success_result(
        f'Repointed {moved} backstore(s) in {saveconfig}'
        + (f', withheld {len(withheld)} whose device is not there: '
           f'{", ".join(sorted(withheld))}' if withheld else ''),
        data, operation_id)
