"""This host as an iSCSI initiator of its own exports.

Exporting a library is only half a test: the other half is an initiator using
it. With the initiator on the same machine - a loopback login to 127.0.0.1 -
the whole path can be exercised from the GUI: attach, move a tape through the
exported changer, write and read through the exported drive, detach.

The initiator's devices are the same library a second time, under the
initiator's SCSI host. lsscsi.local() keeps them out of everything that looks
for a library's own devices; this module is the one place that looks for them
on purpose, and it identifies each by what the target says it exports - LUN
index, backstore, the SCSI address that backstore is bound to, device.conf -
never by the initiator-side address, which starts again from 0:0:0.

iscsiadm is in the packaged sudoers rules (packaging/rpm/mhvtl-gui.sudoers), so
the installed GUI can attach as well as the development server.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..config.service import ConfigService
from ..core import ServiceResult, failure_result, shell, success_result
from ..scsi import lsscsi
from . import bindings, targetcli

logger = logging.getLogger(__name__)

SESSIONS = Path('/sys/class/iscsi_session')
LOOPBACK = '127.0.0.1'


INITIATOR_NAME = Path('/etc/iscsi/initiatorname.iscsi')


def local_name() -> Optional[str]:
    """This host's initiator IQN - what an ACL has to name to let it in."""
    try:
        for line in INITIATOR_NAME.read_text().splitlines():
            if line.strip().startswith('InitiatorName='):
                return line.split('=', 1)[1].strip() or None
    except OSError:
        pass
    return None


def sessions() -> List[Dict]:
    """Every iSCSI session on this host: the target, and the SCSI host it made."""
    found = []
    for session in sorted(SESSIONS.glob('session*')):
        try:
            iqn = (session / 'targetname').read_text().strip()
        except OSError:
            continue
        host = next((int(part[4:]) for part in Path(os.path.realpath(session)).parts
                     if part.startswith('host') and part[4:].isdigit()), None)
        found.append({'session': session.name, 'iqn': iqn, 'host': host})
    return found


def session_for(iqn: str) -> Optional[Dict]:
    return next((s for s in sessions() if s['iqn'] == iqn), None)


def attached_devices(iqn: str, devices=None) -> List[Dict]:
    """The initiator-side devices of one target, by LUN."""
    session = session_for(iqn)
    if session is None or session['host'] is None:
        return []
    devices = devices if devices is not None else lsscsi.discover()
    return [{'lun': device.address.lun, 'type': device.device_type,
             'vendor': device.vendor, 'model': device.model,
             'device_path': device.device_path, 'generic_path': device.generic_path}
            for device in devices
            if device.address and device.address.host == session['host']]


def exported_as(iqn: str, config=None, addresses=None, conf=None) -> Dict[int, Dict]:
    """LUN index -> what it is on this host: the library, and the changer or drive.

    From the target's own configuration, so it does not depend on the order
    the initiator happened to number anything in.
    """
    config = config if config is not None else bindings.running_config()
    addresses = addresses if addresses is not None else bindings.bound_addresses()
    conf = conf if conf is not None else ConfigService().device_conf()
    if not config or conf is None:
        return {}
    owned = bindings.owners(conf)
    luns = {}
    for target in config.get('targets') or []:
        if target.get('wwn') != iqn:
            continue
        for tpg in target.get('tpgs') or []:
            for lun in tpg.get('luns') or []:
                name = (lun.get('storage_object') or '').rsplit('/', 1)[-1]
                owner = owned.get(addresses.get(name))
                if owner is None:
                    continue
                library_id, unit = owner
                role = 'changer' if unit.startswith('vtllibrary@') else 'drive'
                device_id = int(unit.split('@', 1)[1].split('.', 1)[0])
                luns[int(lun['index'])] = {'library_id': library_id, 'role': role,
                                           'device_id': device_id, 'backstore': name}
    return luns


def library_paths(library_id: int, drive_id: int) -> Optional[Dict]:
    """The initiator-side changer and drive of a library, if it is attached here.

    {'iqn', 'host', 'changer': /dev/sgN, 'drive': /dev/stN} for the first
    attached target that exports both; None when none does.
    """
    config = bindings.running_config()
    addresses = bindings.bound_addresses()
    conf = ConfigService().device_conf()
    devices = lsscsi.discover()
    for session in sessions():
        meaning = exported_as(session['iqn'], config, addresses, conf)
        attached = {d['lun']: d for d in attached_devices(session['iqn'], devices)}
        changer = drive = None
        for lun, what in meaning.items():
            if what['library_id'] != int(library_id) or lun not in attached:
                continue
            if what['role'] == 'changer':
                changer = attached[lun]['generic_path']
            elif what['device_id'] == int(drive_id):
                drive = attached[lun]['device_path'] or attached[lun]['generic_path']
        if changer and drive:
            return {'iqn': session['iqn'], 'host': session['host'],
                    'changer': changer, 'drive': drive}
    return None


def portal_for(iqn: str, config=None) -> str:
    """Where this host reaches one of its own targets: ip:port.

    From the target's portals. One listening on all addresses is reached on
    127.0.0.1 at its port; a loopback portal as it is. A target can only listen
    on 127.0.0.1:3260 while nothing listens on 0.0.0.0:3260, so one made by hand
    may well use another port.
    """
    config = config if config is not None else bindings.running_config()
    portals = [portal for target in (config or {}).get('targets') or []
               if target.get('wwn') == iqn
               for tpg in target.get('tpgs') or []
               for portal in tpg.get('portals') or []]
    for portal in portals:
        address, port = str(portal.get('ip_address', '')), int(portal.get('port', 3260))
        if address in ('0.0.0.0', '::', '[::]'):
            return f'{LOOPBACK}:{port}'
        if address.startswith('127.'):
            return f'{address}:{port}'
    if portals:
        return f"{portals[0].get('ip_address')}:{int(portals[0].get('port', 3260))}"
    return f'{LOOPBACK}:3260'


def chap_for(iqn: str, initiator: str = None, config=None) -> Optional[Dict]:
    """The CHAP credentials this host has to log in to one of its own targets
    with, or None when the target does not require any.

    From the target's own configuration: the ACL for this host's initiator
    name if it has credentials, otherwise the target-wide ones, which apply to
    initiators let in by "Allow all initiators". {'required': bool, 'userid',
    'password', 'mutual_userid', 'mutual_password'}.
    """
    config = config if config is not None else bindings.running_config()
    initiator = initiator or local_name()
    for target in (config or {}).get('targets') or []:
        if target.get('wwn') != iqn:
            continue
        for tpg in target.get('tpgs') or []:
            attributes = tpg.get('attributes') or {}
            required = bool(attributes.get('authentication'))
            acl = next((a for a in tpg.get('node_acls') or []
                        if a.get('node_wwn') == initiator), None)
            # LIO uses an initiator's own ACL whenever it has one; the
            # target-wide credentials are only for initiators without one,
            # let in by generate_node_acls. Offering the target-wide ones for
            # an initiator with an ACL fails the login.
            if acl is not None:
                source = acl if acl.get('chap_userid') else None
            elif attributes.get('generate_node_acls') and tpg.get('chap_userid'):
                source = tpg
            else:
                source = None
            if source is None:
                return {'required': required} if required else None
            return {'required': required, 'userid': source.get('chap_userid'),
                    'password': source.get('chap_password'),
                    'mutual_userid': source.get('chap_mutual_userid') or '',
                    'mutual_password': source.get('chap_mutual_password') or ''}
    return None


def attach(iqn: str, portal: str = None, *, wait: float = 20) -> ServiceResult:
    """Log this host in to one of its own targets, at the portal it listens on."""
    operation_id = str(uuid.uuid4())[:8]
    targetcli.validate_iqn(iqn)
    portal = portal or portal_for(iqn)
    if session_for(iqn):
        return success_result(f'{iqn} is already attached on this host',
                              {'devices': attached_devices(iqn)}, operation_id)

    # A node record for this target alone, never logged in at boot. Discovery
    # (sendtargets) was used here first: it records every target the portal
    # offers, with the distribution's default node.startup=automatic, so after
    # a reboot this host logged itself in to library 10's export, which nobody
    # had attached.
    node = ['iscsiadm', '-m', 'node', '-T', iqn, '-p', portal]
    made = shell.sudo([*node, '-o', 'new'], timeout=30)
    if not made.ok:
        return failure_result(f'Could not record {iqn} at {portal}',
                              [made.output.strip()[:300]], operation_id)
    manual = shell.sudo([*node, '-o', 'update', '-n', 'node.startup', '-v', 'manual'],
                        timeout=30)
    if not manual.ok:
        shell.sudo([*node, '-o', 'delete'], timeout=30)
        return failure_result(f'Could not make {iqn} manual-start; not attached',
                              [manual.output.strip()[:300]], operation_id)

    # CHAP: the target's own credentials for this host go into the node
    # record, so a target that requires them can still be attached here.
    chap = chap_for(iqn)
    if chap and chap.get('required') and not chap.get('userid'):
        shell.sudo([*node, '-o', 'delete'], timeout=30)
        return failure_result(
            f'{iqn} requires CHAP, but has no credentials for this host',
            [f'set CHAP on the ACL for {local_name()}, or target-wide'], operation_id)
    if chap and chap.get('userid'):
        settings = [('node.session.auth.authmethod', 'CHAP'),
                    ('node.session.auth.username', chap['userid']),
                    ('node.session.auth.password', chap['password'])]
        if chap.get('mutual_userid'):
            settings += [('node.session.auth.username_in', chap['mutual_userid']),
                         ('node.session.auth.password_in', chap['mutual_password'])]
        for name, value in settings:
            done = shell.sudo([*node, '-o', 'update', '-n', name, '-v', value], timeout=30)
            if not done.ok:
                shell.sudo([*node, '-o', 'delete'], timeout=30)
                return failure_result(f'Could not set {name} for {iqn}; not attached',
                                      [done.output.strip()[:300]], operation_id)
    login = shell.sudo([*node, '--login'], timeout=60)
    if not login.ok:
        shell.sudo([*node, '-o', 'delete'], timeout=30)
        return failure_result(f'Could not log in to {iqn}',
                              [login.output.strip()[:300]], operation_id)

    # The initiator scans the LUNs after the login returns.
    expected = len(exported_as(iqn))
    deadline = time.monotonic() + wait
    devices = attached_devices(iqn)
    while len(devices) < expected and time.monotonic() < deadline:
        time.sleep(1)
        devices = attached_devices(iqn)
    how = ''
    if chap and chap.get('userid'):
        how = ' with mutual CHAP' if chap.get('mutual_userid') else ' with CHAP'
    return success_result(f'Attached {iqn} on this host{how}: {len(devices)} device(s)',
                          {'devices': devices, 'session': session_for(iqn),
                           'chap': bool(chap and chap.get('userid'))},
                          operation_id)


def detach(iqn: str) -> ServiceResult:
    """Log out, and forget the node so nothing logs it in again at boot.

    Every portal of the target: the one it was attached through is in the
    node record, whatever port that was.
    """
    operation_id = str(uuid.uuid4())[:8]
    targetcli.validate_iqn(iqn)
    if session_for(iqn) is None:
        return success_result(f'{iqn} is not attached', {}, operation_id)
    logout = shell.sudo(['iscsiadm', '-m', 'node', '-T', iqn, '--logout'], timeout=60)
    if not logout.ok:
        return failure_result(f'Could not log out of {iqn}',
                              [logout.output.strip()[:300]], operation_id)
    shell.sudo(['iscsiadm', '-m', 'node', '-T', iqn, '-o', 'delete'], timeout=30)
    return success_result(f'Detached {iqn} from this host', {}, operation_id)
