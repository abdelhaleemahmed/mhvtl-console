"""Exporting a whole library over iSCSI, in one step.

Moved from iscsi_service.py:export_library. It is a sequence, not an operation:
a target, then a pscsi backstore per device, then a LUN per backstore, then the
access rule. Any of those can fail on its own, and what the operator needs to
know is which - a target with three of its four drives exported is a working
library that will stall on the fourth.

The same shape as libraries/workflow.py, and for the same reason: this lived
inside a view, so there was no way to export a library except by submitting the
form, and no way to report the steps except as flash messages.

    report = export_library(10, devices)
    report.ok            # every step succeeded
    report.steps         # what happened, in order
    report.warnings      # what did not, without stopping the rest

A note on LUN order. The changer is exported as LUN 0 and the drives follow.
Backup applications look for the medium changer at the lowest LUN, and some
will not scan past a tape drive to find it, so the order is deliberate rather
than whatever the caller happened to pass.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from ..core import ServiceResult, failure_result, success_result
from . import bindings, targetcli
from .service import IscsiService

logger = logging.getLogger(__name__)


@dataclass
class ExportReport:
    """What the export did, step by step."""
    iqn: str
    library_id: int
    steps: List[Dict] = field(default_factory=list)
    backstores: List[str] = field(default_factory=list)
    luns: List[Dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def record(self, step: str, ok: bool, detail: str = '') -> None:
        self.steps.append({'step': step, 'ok': ok, 'detail': detail})
        if not ok:
            self.warnings.append(f'{step}: {detail}' if detail else step)

    def to_dict(self) -> Dict:
        return {'iqn': self.iqn, 'library_id': self.library_id,
                'steps': self.steps, 'backstores': self.backstores,
                'luns': self.luns, 'warnings': self.warnings,
                'errors': self.errors, 'ok': self.ok}


def default_iqn(library_id: int) -> str:
    """iqn.<year>-<month>.com.mhvtl:library<N>.

    The date is the naming authority's, not today's event: RFC 3720 wants the
    year and month at which the naming authority owned the domain. Generating
    it from today is what MHVTL's own examples do and what every target on this
    host already uses, so it stays.
    """
    return (f'iqn.{datetime.now().strftime("%Y-%m")}.com.mhvtl:'
            f'library{int(library_id)}')


def export_library(library_id: int, devices: List[Dict], *, iqn: str = None,
                   allow_all_initiators: bool = True,
                   initiator_iqn: str = None,
                   service: IscsiService = None) -> ServiceResult:
    """Create a target exporting a library's changer and drives.

    Args:
        library_id: names the target and its backstores.
        devices: dicts with device_path, and optionally name and type. A device
            whose type is "changer" is exported first, as LUN 0.
        iqn: the target name; generated from the library id when not given.
        allow_all_initiators: sets generate_node_acls, which lets anything that
            connects attach. False with an initiator_iqn creates an ACL for
            that initiator alone.

    The target is created first and is not rolled back if a later step fails:
    an operator with a half-exported library wants to add the missing drive,
    not to start again. What failed is in the report.
    """
    operation_id = str(uuid.uuid4())[:8]
    service = service or IscsiService()
    iqn = iqn or default_iqn(library_id)
    report = ExportReport(iqn=iqn, library_id=int(library_id))

    created = service.create_target(iqn)
    report.record('create target', created.success, created.message)
    if not created.success:
        report.errors.extend(created.errors or [created.message])
        return failure_result(f'Could not create target {iqn}',
                              report.errors, operation_id)

    drive_number = 0
    for lun_id, device in enumerate(_changer_first(devices)):
        path = device.get('device_path')
        # lib<L>_changer and lib<L>_drive<N> are what remap.py reads to repoint
        # a saved backstore after a reboot; any other name it leaves alone.
        if (device.get('type') or '').lower() == 'changer':
            default = f'lib{library_id}_changer'
        else:
            default = f'lib{library_id}_drive{drive_number}'
            drive_number += 1
        name = device.get('name') or default
        if not path:
            report.record(f'export {name}', False, 'no device_path')
            continue

        backstore = service.create_backstore(path, name, plugin='pscsi',
                                             record_binding=False)
        report.record(f'backstore {name}', backstore.success, backstore.message)
        if not backstore.success:
            continue
        report.backstores.append(name)

        lun = service.create_lun(iqn, 'pscsi', name)
        report.record(f'lun {lun_id} -> {name}', lun.success, lun.message)
        if lun.success:
            report.luns.append({'lun_id': lun_id, 'backstore': name,
                                'device_path': path})

    _apply_access(service, iqn, report, allow_all_initiators, initiator_iqn)

    if not report.luns:
        report.errors.append('the target was created but exports no devices')
        return failure_result(
            f'Target {iqn} was created but nothing was exported',
            report.errors + report.warnings, operation_id)

    message = (f'Library {library_id} exported as {iqn}: '
               f'{len(report.luns)} LUN(s)')
    if report.warnings:
        message += f', {len(report.warnings)} step(s) failed'
    return success_result(message, report.to_dict(), operation_id)


def _changer_first(devices: List[Dict]) -> List[Dict]:
    """The medium changer as LUN 0, then the drives in the order given.

    Some backup applications stop scanning at the first device they do not
    recognise, so a changer behind three tape drives is a library they report as
    having no robot.
    """
    changers = [d for d in devices if (d.get('type') or '').lower() == 'changer']
    others = [d for d in devices if (d.get('type') or '').lower() != 'changer']
    return changers + others


def _apply_access(service: IscsiService, iqn: str, report: ExportReport,
                  allow_all: bool, initiator_iqn: Optional[str]) -> None:
    """Open the target to everyone, or to one named initiator."""
    if allow_all:
        result = service.set_generate_node_acls(iqn, True)
        report.record('allow any initiator', result.success, result.message)
        return

    result = service.set_generate_node_acls(iqn, False)
    report.record('require an ACL', result.success, result.message)
    if initiator_iqn:
        acl = service.create_acl(iqn, initiator_iqn)
        report.record(f'allow {initiator_iqn}', acl.success, acl.message)
    else:
        report.warnings.append(
            'ACLs are required but none was added, so no initiator can attach')


#: A backstore this library's export made: lib<L>_changer, lib<L>_drive<N>.
#: The same names workflow writes above and remap.py reads; anything else in
#: the configuration was made by hand and is left alone.
#: The names this code gives a library's backstores, and the only ones it
#: will remove. One expression, used by both readers below: written twice,
#: they disagreed - the other accepted lib50_drivefoo.
OUR_BACKSTORE = re.compile(r'^lib(\d+)_(changer|drive\d+)$')


def _our_backstores(library_id: int, backstores: List[Dict]) -> List[Dict]:
    """The backstores this library's export made, and nothing else."""
    ours = []
    for backstore in backstores:
        match = OUR_BACKSTORE.match(backstore.get('name') or '')
        if match and int(match.group(1)) == int(library_id):
            ours.append(backstore)
    return ours


def library_of(target: Dict) -> Optional[int]:
    """Which library a target exports, or None when it is not one of ours.

    Read from the backstores its LUNs use - lib<L>_changer, lib<L>_drive<N> -
    rather than from the target's name. The name is only a default: an export
    made with --iqn says nothing about the library, while the backstores are
    what this code wrote and what remap reads.

    A target whose LUNs point at more than one library is not one library's
    export, and answers None.
    """
    found = set()
    for tpg in target.get('tpgs') or []:
        for lun in tpg.get('luns') or []:
            name = lun.get('backstore_name') or ''
            match = OUR_BACKSTORE.match(name)
            if match:
                found.add(int(match.group(1)))
    return found.pop() if len(found) == 1 else None


def unexport_library(library_id: int, *, iqn: str = None,
                     remove_backstores: bool = True,
                     service: IscsiService = None) -> ServiceResult:
    """Stop exporting a library: its target, and the devices it exported.

    The inverse of export_library, and it exists because deleting the target
    alone is not what an operator means. A target carries its LUNs and its
    ACLs away with it; the pscsi backstores stay, holding the /dev/sg node
    each was made with. That is right for a backstore on its own - the same
    device can be mapped into another target - and wrong for a library that
    is no longer exported: the next library to take that SCSI address wants
    the node, and `iscsi remap` goes on trying to repoint a backstore at a
    daemon that no longer exists.

    Only backstores this export named are removed - lib<L>_changer and
    lib<L>_drive<N>. One made by hand with targetcli has a name we do not
    recognise, and removing it is not ours to decide.

    remove_backstores=False deletes the target and leaves them, which is what
    the old behaviour was; it is here for a caller that means it.
    """
    operation_id = str(uuid.uuid4())[:8]
    service = service or IscsiService()
    iqn = iqn or default_iqn(library_id)
    report = ExportReport(iqn=iqn, library_id=int(library_id))

    listed = service.targets()
    known = {t.get('iqn') for t in (listed.data or {}).get('targets', [])} \
        if listed.success else set()

    if iqn in known:
        deleted = service.delete_target(iqn)
        report.record('delete target', deleted.success, deleted.message)
        if not deleted.success:
            report.errors.extend(deleted.errors or [deleted.message])
            return failure_result(f'Could not delete target {iqn}',
                                  report.errors, operation_id)
    else:
        # Not an error: the target may already be gone, and the backstores it
        # left are exactly what this is for.
        report.record('delete target', True, f'{iqn} was not there')

    removed = []
    if remove_backstores:
        found = service.backstores()
        ours = _our_backstores(library_id, (found.data or {}).get('backstores', [])) \
            if found.success else []
        for backstore in ours:
            name = backstore.get('name')
            gone = service.delete_backstore(backstore.get('plugin') or 'pscsi', name)
            report.record(f'remove backstore {name}', gone.success, gone.message)
            if gone.success:
                removed.append(name)

        if removed:
            forgotten = bindings.forget(removed)
            report.record('forget the bindings', forgotten,
                          '' if forgotten else 'the record could not be written')

    saved = service.save_config()
    report.record('save the configuration', saved.success, saved.message)

    data = report.to_dict()
    data['backstores_removed'] = removed
    message = f'Library {library_id} is no longer exported'
    if removed:
        message += f'; {len(removed)} backstore(s) removed'
    if report.warnings:
        message += f' ({len(report.warnings)} warning(s))'
    result = success_result(message, data, operation_id)
    return result
