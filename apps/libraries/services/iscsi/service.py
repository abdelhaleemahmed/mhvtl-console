"""iSCSI orchestration: status, targets, LUNs, ACLs, exporting a library.

Mutations go through targetcli.py, so the IQN and device-path checks described
there apply to every one of them. Reading goes through parsing.py, which walks
LIO's two formats: /etc/target/saveconfig.json first and the `targetcli ls` tree
when the JSON cannot be read. Both are pinned against real output from a host
with a library exported, the way the device.conf and mtx parsers are.

export_library still delegates: it is a multi-step sequence - a backstore per
device, a target, a LUN per backstore - and it belongs in a workflow module next
to create_library_workflow rather than being inlined here.

What this module owns is the shape of the answer: every method returns a
ServiceResult from core.results rather than iscsi_service.py's own rival class.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from typing import Dict, List, Optional

from ..core import ServiceResult, failure_result, shell, success_result
from ..console import units
from . import bindings, parsing, targetcli

#: Where targetctl saves and restores the LIO configuration.
CONFIG_PATH = '/etc/target/saveconfig.json'

#: The unit that restores that configuration into the kernel at boot.
SERVICE_UNIT = 'target.service'

logger = logging.getLogger(__name__)


class IscsiService:
    """Exporting libraries and drives over iSCSI.

        IscsiService().status()
        IscsiService().create_target('iqn.2026-01.com.example:library10')
    """

    # -- reading -----------------------------------------------------------

    def status(self) -> ServiceResult:
        """Service state, targets and backstores in one answer."""
        operation_id = str(uuid.uuid4())[:8]
        try:
            targets, backstores, source = self._configuration()
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('reading iSCSI status')
            return failure_result(f'Could not read the iSCSI status: {exc}',
                                  [str(exc)], operation_id)

        running = units.is_active(SERVICE_UNIT)
        # A target with LUNs in the saved configuration and none in the running
        # tree is the pscsi-after-reload case parsing.py describes; saying so
        # here is the difference between "exported" and "configured once".
        exported = sum(len(tpg.luns) for target in targets for tpg in target.tpgs)

        return success_result(
            f'iSCSI {"running" if running else "stopped"}, {len(targets)} '
            f'target{"s" if len(targets) != 1 else ""}, {exported} LUN(s)',
            {'service': {'running': running,
                         'enabled': units.is_enabled(SERVICE_UNIT),
                         'unit': SERVICE_UNIT},
             'targets': [target.to_dict() for target in targets],
             'backstores': [backstore.to_dict() for backstore in backstores],
             'lun_count': exported,
             'config_saved': shell.sudo(['test', '-f', CONFIG_PATH]).ok,
             'source': source},
            operation_id)

    def _configuration(self):
        """Targets and backstores, from the JSON if it can be read and from the
        printed tree if it cannot.

        Both are tried before giving up: a host that has never run saveconfig
        has no JSON but a perfectly readable tree, and one whose target service
        is stopped has the JSON and an empty tree.
        """
        saved = shell.sudo_cat(CONFIG_PATH)
        if saved.ok:
            parsed = parsing.parse_config(saved.stdout)
            if parsed['readable']:
                return parsed['targets'], parsed['backstores'], 'saveconfig.json'

        tree = targetcli.ls()
        if not tree.ok:
            raise RuntimeError(
                f'neither {CONFIG_PATH} nor `targetcli ls` could be read: '
                f'{tree.output.strip()[:200]}')

        return (parsing.targets_from_ls(tree.stdout),
                parsing.backstores_from_ls(tree.stdout), 'targetcli ls')

    def targets(self) -> ServiceResult:
        operation_id = str(uuid.uuid4())[:8]
        try:
            found, _, source = self._configuration()
        except Exception as exc:                       # noqa: BLE001 - reported
            return failure_result(f'Could not list targets: {exc}', [str(exc)],
                                  operation_id)
        return success_result(f'{len(found)} target{"s" if len(found) != 1 else ""}',
                              {'targets': [t.to_dict() for t in found],
                               'count': len(found), 'source': source}, operation_id)

    def backstores(self) -> ServiceResult:
        operation_id = str(uuid.uuid4())[:8]
        try:
            _, found, source = self._configuration()
        except Exception as exc:                       # noqa: BLE001 - reported
            return failure_result(f'Could not list backstores: {exc}', [str(exc)],
                                  operation_id)
        return success_result(f'{len(found)} backstore{"s" if len(found) != 1 else ""}',
                              {'backstores': [b.to_dict() for b in found],
                               'count': len(found), 'source': source}, operation_id)

    def available_devices(self) -> ServiceResult:
        """The MHVTL devices that can be exported.

        Only the generic (/dev/sg) node is usable for a pscsi backstore, so a
        device without one is not offered. Moved from the old adapter
        (once adapters/iscsi_service.get_available_scsi_devices).
        """
        from ..scsi import lsscsi

        operation_id = str(uuid.uuid4())[:8]
        try:
            discovered = lsscsi.local()
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.warning('discovering SCSI devices for export: %s', exc)
            return failure_result(f'Could not list SCSI devices: {exc}',
                                  [str(exc)], operation_id)
        # Which library each device is, and the backstore name remap.py
        # understands for it: lib<L>_changer, lib<L>_drive<N>. A backstore
        # named anything else is not repointed after a reboot.
        from ..config.service import ConfigService
        from .remap import NAME_RE, expected_addresses
        conf = ConfigService().device_conf()
        names = ({address: name for name, address in expected_addresses(conf).items()}
                 if conf is not None else {})
        devices = []
        for device in discovered:
            if device.device_type not in ('mediumx', 'tape') or not device.generic_path:
                continue
            name = names.get(device.address.ctl) if device.address else None
            match = NAME_RE.match(name) if name else None
            library_id = int(match['library']) if match else None
            devices.append({
                'host': str(device.address),
                'type': 'changer' if device.device_type == 'mediumx' else 'tape',
                'vendor': device.vendor,
                'model': device.model,
                'device_path': device.device_path or device.generic_path,
                'generic_path': device.generic_path,
                'library_id': library_id,
                'belongs_to': (f'library {library_id} changer' if match and match['changer']
                               else f'library {library_id} drive {int(match["drive"])}'
                               if match else ''),
                'suggested_name': name or f'{device.vendor}_{device.model}'.replace(' ', '_'),
            })
        return success_result(f'{len(devices)} exportable device(s)',
                              {'devices': devices, 'count': len(devices)}, operation_id)

    # -- saved configuration -------------------------------------------------

    def save_config(self) -> ServiceResult:
        """Write the running configuration to saveconfig.json."""
        operation_id = str(uuid.uuid4())[:8]
        result = targetcli.save_config()
        if not result.ok:
            return failure_result('Could not save the iSCSI configuration',
                                  [result.output.strip()[:300]], operation_id)
        return success_result('Configuration saved', {'path': CONFIG_PATH}, operation_id)

    def restore_config(self) -> ServiceResult:
        """Load the saved configuration back into the kernel.

        A pscsi backstore names a /dev/sgN path, and those numbers change when
        the SCSI modules reload, so a restore can bring back a target that
        exports the wrong device; remap.py fixes that by SCSI address.
        """
        operation_id = str(uuid.uuid4())[:8]
        result = targetcli.restore_config()
        if not result.ok:
            return failure_result('Could not restore the iSCSI configuration',
                                  [result.output.strip()[:300]], operation_id)
        return success_result('Configuration restored',
                              {'output': result.stdout.strip()}, operation_id)

    # -- mutations (validated here) ---------------------------------------

    def create_target(self, iqn: str) -> ServiceResult:
        return self._targetcli(lambda: targetcli.create_target(iqn),
                               f'Created target {iqn}', save=True)

    def delete_target(self, iqn: str) -> ServiceResult:
        return self._targetcli(lambda: targetcli.delete_target(iqn),
                               f'Deleted target {iqn}', save=True)

    def create_backstore(self, device_path: str, name: str, *,
                         plugin: str = 'pscsi',
                         allow_any_device: bool = False,
                         record_binding: bool = True) -> ServiceResult:
        """Export a device.

        The device is checked against the MHVTL devices first: exporting a disk
        that is not a virtual tape device publishes it to any initiator that
        connects. A pscsi backstore is recorded against the daemon it bound to,
        so a later restart of that daemon shows up as stale (see bindings.py);
        export_library records its backstores once, at the end.
        """
        maker = (targetcli.create_block_backstore if plugin == 'block'
                 else targetcli.create_pscsi_backstore)
        result = self._targetcli(
            lambda: maker(device_path, name, allow_any_device=allow_any_device),
            f'Created {plugin} backstore {name} for {device_path}', save=True)
        if result.success and plugin == 'pscsi' and record_binding \
                and bindings.rebind_allowed():
            bindings.record([name])
        return result

    def delete_backstore(self, plugin: str, name: str) -> ServiceResult:
        return self._targetcli(lambda: targetcli.delete_backstore(plugin, name),
                               f'Deleted {plugin} backstore {name}', save=True)

    def create_lun(self, iqn: str, plugin: str, name: str, tpg: int = 1,
                   lun: int = None) -> ServiceResult:
        """Map a backstore into a target, at LUN `lun` or the next free one."""
        where = f' as LUN {int(lun)}' if lun is not None else ''
        return self._targetcli(lambda: targetcli.create_lun(iqn, plugin, name, tpg, lun=lun),
                               f'Added {name} to {iqn}{where}', save=True)

    def delete_lun(self, iqn: str, lun_id: int, tpg: int = 1) -> ServiceResult:
        return self._targetcli(lambda: targetcli.delete_lun(iqn, lun_id, tpg),
                               f'Removed lun{lun_id} from {iqn}', save=True)

    def create_acl(self, iqn: str, initiator_iqn: str, tpg: int = 1) -> ServiceResult:
        return self._targetcli(lambda: targetcli.create_acl(iqn, initiator_iqn, tpg),
                               f'Allowed {initiator_iqn} on {iqn}', save=True)

    def delete_acl(self, iqn: str, initiator_iqn: str, tpg: int = 1) -> ServiceResult:
        return self._targetcli(lambda: targetcli.delete_acl(iqn, initiator_iqn, tpg),
                               f'Removed {initiator_iqn} from {iqn}', save=True)

    def create_portal(self, iqn: str, ip: str = '0.0.0.0', port: int = 3260,
                      tpg: int = 1) -> ServiceResult:
        return self._targetcli(lambda: targetcli.create_portal(iqn, ip, port, tpg),
                               f'Listening for {iqn} on {ip}:{port}', save=True)

    def delete_portal(self, iqn: str, ip: str = '0.0.0.0', port: int = 3260,
                      tpg: int = 1) -> ServiceResult:
        """Stop listening for a target on one address.

        Removing the last portal leaves a target nothing can connect to, which
        from an initiator's side looks exactly like the target being gone.
        """
        return self._targetcli(
            lambda: targetcli.delete_portal(iqn, ip, port, tpg),
            f'{iqn} no longer listens on {ip}:{port}', save=True)

    def set_generate_node_acls(self, iqn: str, enabled: bool,
                               tpg: int = 1) -> ServiceResult:
        """Let any initiator attach, or require an ACL.

        Turning this on removes the access control on a target, so it is worth
        an explicit call rather than a flag buried in a form.
        """
        return self._targetcli(
            lambda: targetcli.set_attribute(iqn, 'generate_node_acls',
                                            '1' if enabled else '0', tpg),
            f'{"Any initiator may now attach to" if enabled else "ACLs now required for"} {iqn}',
            save=True)

    def export_library(self, library_id: int, devices: List[Dict], *,
                       iqn: str = None, allow_all_initiators: bool = True,
                       initiator_iqn: str = None) -> ServiceResult:
        """Export a library - its changer and every drive - as one target.

        Implemented in workflow.py; see it for the step order and why the
        changer goes out as LUN 0.
        """
        from . import workflow
        result = workflow.export_library(
            library_id, self._with_device_types(devices), iqn=iqn,
            allow_all_initiators=allow_all_initiators,
            initiator_iqn=initiator_iqn, service=self)
        if result.success and (result.data or {}).get('backstores') \
                and bindings.rebind_allowed():
            bindings.record(result.data['backstores'])
        return result

    def unexport_library(self, library_id: int, *, iqn: str = None,
                         remove_backstores: bool = True) -> ServiceResult:
        """Stop exporting a library: the target, and the devices it exported.

        Implemented in workflow.py; see it for why the backstores go too.
        """
        from . import workflow
        return workflow.unexport_library(
            library_id, iqn=iqn, remove_backstores=remove_backstores,
            service=self)

    def binding_status(self, library_id: int = None) -> ServiceResult:
        """Whether each exported device is still bound to a running daemon."""
        return bindings.status(library_id)

    def rebind_library(self, library_id: int) -> ServiceResult:
        """Bind a library's exports to the daemons running now; see bindings.py."""
        return bindings.rebind(library_id, wait=0)

    def _with_device_types(self, devices: List[Dict]) -> List[Dict]:
        """Fill in each device's type from discovery where the caller left it out.

        The export form posts paths and names but not types; the workflow
        exports the changer as LUN 0, so it needs to know which one it is.
        """
        if all(device.get('type') for device in devices):
            return list(devices)
        found = self.available_devices()
        known = {}
        for device in (found.data or {}).get('devices', []):
            known[device['device_path']] = device['type']
            known[device['generic_path']] = device['type']
        filled = []
        for device in devices:
            entry = dict(device)
            entry.setdefault('type', known.get(entry.get('device_path'), 'tape'))
            filled.append(entry)
        return filled

    # -- service control ---------------------------------------------------

    def service(self, action: str) -> ServiceResult:
        """start, stop, restart, enable or disable target.service.

        The action is matched against a fixed set rather than passed through to
        systemctl: this is reachable from a web form, and `mask` or `isolate`
        are not things a form should be able to ask for.
        """
        operation_id = str(uuid.uuid4())[:8]
        control = {'start': units.start, 'stop': units.stop,
                   'restart': units.restart, 'enable': units.enable,
                   'disable': units.disable}.get(action)
        past = {'start': 'started', 'stop': 'stopped', 'restart': 'restarted',
                'enable': 'enabled', 'disable': 'disabled'}.get(action, action)
        if control is None:
            return failure_result(f'Unknown action {action!r}',
                                  ['expected start, stop, restart, enable or '
                                   'disable'], operation_id)

        result = control(SERVICE_UNIT)
        if not result.ok:
            return failure_result(f'Could not {action} {SERVICE_UNIT}',
                                  [result.output.strip()[:300]], operation_id)
        return success_result(f'{SERVICE_UNIT} {past}',
                              {'unit': SERVICE_UNIT, 'action': action}, operation_id)

    # -- helpers -----------------------------------------------------------

    # -- CHAP -----------------------------------------------------------------

    def set_chap(self, iqn: str, userid: str, password: str, *, initiator: str = None,
                 mutual_userid: str = '', mutual_password: str = '',
                 tpg: int = 1) -> ServiceResult:
        """Require CHAP, for one initiator's ACL or target-wide.

        With `initiator`, the credentials are that ACL's. Without it they are
        the target's own, used by any initiator that comes in through "Allow
        all initiators" - which is the only way to put a password on an open
        target. Mutual CHAP adds the target proving itself to the initiator.
        Either way the TPG's authentication attribute is switched on, which is
        what makes LIO refuse a login without the password.
        """
        operation_id = str(uuid.uuid4())[:8]
        try:
            targetcli.validate_chap(userid, password, mutual_userid, mutual_password)
        except targetcli.InvalidChap as exc:
            return failure_result(str(exc), [str(exc)], operation_id)
        who = f'{initiator}' if initiator else 'any initiator without an ACL'
        steps = (
            (lambda: targetcli.set_auth(iqn, initiator=initiator, userid=userid,
                                        password=password, mutual_userid=mutual_userid,
                                        mutual_password=mutual_password, tpg=tpg),
             'set the credentials'),
            (lambda: targetcli.set_attribute(iqn, 'authentication', '1', tpg),
             'require authentication'),
        )
        for action, what in steps:
            result = self._targetcli(action, what)
            if not result.success:
                return failure_result(f'CHAP was not set on {iqn}: could not {what}',
                                      result.errors, operation_id)
        saved = targetcli.save_config()
        message = (f'CHAP{" (mutual)" if mutual_userid else ""} required on {iqn} '
                   f'for {who}, as user {userid}')
        data = {'iqn': iqn, 'initiator': initiator, 'userid': userid,
                'mutual': bool(mutual_userid)}
        if not saved.ok:
            data['warning'] = 'saveconfig failed; the change will be lost on reboot'
        return success_result(message, data, operation_id)

    def clear_chap(self, iqn: str, *, initiator: str = None, tpg: int = 1) -> ServiceResult:
        """Remove CHAP from one ACL or from the target, and stop requiring it
        once no credentials are left anywhere on the target."""
        operation_id = str(uuid.uuid4())[:8]
        cleared = self._targetcli(
            lambda: targetcli.set_auth(iqn, initiator=initiator, tpg=tpg),
            'cleared the credentials')
        if not cleared.success:
            return failure_result(f'CHAP was not cleared on {iqn}', cleared.errors,
                                  operation_id)
        still = self.chap_in_use(iqn, tpg=tpg)
        if not still:
            off = self._targetcli(lambda: targetcli.set_attribute(iqn, 'authentication',
                                                                  '0', tpg),
                                  'stop requiring authentication')
            if not off.success:
                return failure_result(f'CHAP was cleared on {iqn} but it is still '
                                      f'required', off.errors, operation_id)
        targetcli.save_config()
        who = initiator or 'the target'
        return success_result(
            f'CHAP removed for {who}' + ('' if still else
                                         f'; {iqn} no longer requires authentication'),
            {'iqn': iqn, 'initiator': initiator, 'still_required': still}, operation_id)

    def chap_in_use(self, iqn: str, tpg: int = 1) -> bool:
        """Whether any credentials are left on the target or its ACLs."""
        config = bindings.running_config() or {}
        for target in config.get('targets') or []:
            if target.get('wwn') != iqn:
                continue
            for group in target.get('tpgs') or []:
                if int(group.get('tag', 1)) != int(tpg):
                    continue
                if group.get('chap_userid'):
                    return True
                if any(acl.get('chap_userid') for acl in group.get('node_acls') or []):
                    return True
        return False

    def _targetcli(self, action, message: str, *, save: bool = False) -> ServiceResult:
        """Run a validated targetcli mutation and report it uniformly."""
        operation_id = str(uuid.uuid4())[:8]
        try:
            result = action()
        except (targetcli.InvalidIqn, targetcli.DeviceRefused, targetcli.InvalidChap) as exc:
            return failure_result(str(exc), [str(exc)], operation_id)
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('targetcli call failed')
            return failure_result(f'targetcli failed: {exc}', [str(exc)], operation_id)

        if not result.ok:
            return failure_result(f'targetcli refused the change: '
                                  f'{result.output.strip()[:200]}',
                                  [result.output.strip()[:300]], operation_id)

        saved = targetcli.save_config() if save else None
        data = {'output': result.stdout.strip()}
        if saved is not None and not saved.ok:
            data['warning'] = ('The change was made but saveconfig failed; it will '
                               'be lost on reboot')
            logger.warning('targetcli saveconfig failed: %s',
                           saved.output.strip()[:200])
        return success_result(message, data, operation_id)
