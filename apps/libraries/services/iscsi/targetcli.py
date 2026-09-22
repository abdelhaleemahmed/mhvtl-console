"""The targetcli wrapper, and the checks that belong in front of it.

Moved from the command sites in iscsi_service.py, with two validations added.
Both are about the same thing: targetcli exports block devices to the network,
and an argument that reaches it unchecked is a mistake with reach.

DEVICE PATHS ARE CHECKED AGAINST THE MHVTL DEVICES.

    create_block_backstore(device_path, name) passed device_path straight
    through:

        /backstores/block create name=<name> dev=<device_path>

    with no validation at all. An operator who types /dev/sda exports the
    system disk to whatever initiator connects, and until recently that
    endpoint was also CSRF-exempt, so a web page could ask for it on their
    behalf. Paths are now matched against the tape and changer devices MHVTL
    owns; anything else has to be passed with allow_any_device=True by a caller
    that has decided it means it.

IQNs ARE CHECKED AGAINST THEIR FORMAT.

    The old check was iqn.startswith('iqn.'), so 'iqn.' alone was accepted, as
    was 'iqn.$(reboot)'. RFC 3720 shapes an IQN as

        iqn.yyyy-mm.naming-authority[:unique]

    which is what IQN_RE describes.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from typing import List, Optional, Sequence

from ..core import SLOW, CommandResult, shell

logger = logging.getLogger(__name__)

#: iqn.yyyy-mm.reversed.domain[:label] - RFC 3720 section 3.2.6.3.1.
IQN_RE = re.compile(
    r'^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*[a-z0-9](?::[A-Za-z0-9._:-]+)?$')

#: Device nodes MHVTL creates. A backstore may point at one of these.
MHVTL_DEVICE_RE = re.compile(r'^/dev/(s[gt]\d+|nst\d+|sch\d+)$')


#: A CHAP user name: what an initiator sends, often its own IQN.
CHAP_USER_RE = re.compile(r'^[A-Za-z0-9._:@-]{1,64}$')
#: A CHAP secret. 12 to 16 characters is what every initiator accepts (the
#: Microsoft one refuses anything else); no spaces, quotes or '=', which
#: targetcli's own command parser would split or strip.
CHAP_SECRET_RE = re.compile(r'^[A-Za-z0-9._:@+%^~!-]{12,16}$')


class InvalidChap(ValueError):
    """A CHAP user name or secret that would not survive, or not be accepted."""


def validate_chap(userid: str, password: str, mutual_userid: str = '',
                  mutual_password: str = '') -> None:
    if not CHAP_USER_RE.match(userid or ''):
        raise InvalidChap('the CHAP user name is required: letters, digits and . _ : @ -')
    if not CHAP_SECRET_RE.match(password or ''):
        raise InvalidChap('the CHAP password must be 12 to 16 characters, without '
                          'spaces, quotes or "="')
    if bool(mutual_userid) != bool(mutual_password):
        raise InvalidChap('mutual CHAP needs both a user name and a password')
    if mutual_userid:
        if not CHAP_USER_RE.match(mutual_userid):
            raise InvalidChap('the mutual CHAP user name has characters that are not allowed')
        if not CHAP_SECRET_RE.match(mutual_password):
            raise InvalidChap('the mutual CHAP password must be 12 to 16 characters, '
                              'without spaces, quotes or "="')
        if mutual_password == password:
            raise InvalidChap('the mutual CHAP password has to differ from the other one')


class InvalidIqn(ValueError):
    """The IQN is not a well-formed iSCSI Qualified Name."""


class DeviceRefused(ValueError):
    """The device is not one MHVTL owns, and was not explicitly allowed."""


def validate_iqn(iqn: str) -> str:
    """Return the IQN, or explain why it was refused."""
    if not iqn:
        raise InvalidIqn('an IQN is required')
    if not IQN_RE.match(iqn):
        raise InvalidIqn(
            f'{iqn!r} is not a valid IQN. The form is '
            f'iqn.yyyy-mm.reversed.domain:label, for example '
            f'iqn.2026-01.com.example:library10')
    return iqn


def mhvtl_devices() -> List[str]:
    """The device nodes MHVTL currently owns - the only ones worth exporting."""
    from ..scsi import lsscsi

    nodes = []
    for device in lsscsi.local():
        nodes.extend(path for path in (device.device_path, device.generic_path)
                     if path)
    return sorted(set(nodes))


def validate_device(device_path: str, *, allow_any: bool = False) -> str:
    """Check a device path before it is exported to the network.

    Two gates: it has to look like an MHVTL device node, and it has to be one
    this host actually has. Exporting /dev/sda is a decision, not a typo, so a
    caller that means it passes allow_any.
    """
    if not device_path:
        raise DeviceRefused('a device path is required')
    if allow_any:
        logger.warning('exporting %s without the MHVTL device check', device_path)
        return device_path

    if not MHVTL_DEVICE_RE.match(device_path):
        raise DeviceRefused(
            f'{device_path!r} is not an MHVTL device node. Exporting a disk that '
            f'is not a virtual tape device would publish it to any initiator '
            f'that connects.')

    owned = mhvtl_devices()
    if owned and device_path not in owned:
        raise DeviceRefused(
            f'{device_path} is not one of this host\'s MHVTL devices '
            f'({", ".join(owned[:6])}{"..." if len(owned) > 6 else ""})')
    return device_path


def run(args: Sequence[str], *, timeout: int = SLOW) -> CommandResult:
    """Run one targetcli command.

    An argument list, never a shell string: a backstore name arrives from a form.
    """
    return shell.sudo(['targetcli', *[str(arg) for arg in args]], timeout=timeout)


def save_config() -> CommandResult:
    """Persist the running configuration, so it survives a reboot."""
    return run(['saveconfig'])


def restore_config() -> CommandResult:
    return run(['restoreconfig'])


def ls(path: str = '/') -> CommandResult:
    return run([path, 'ls'])


# -- the mutations, each with its check in front ---------------------------

def create_block_backstore(device_path: str, name: str, *,
                           allow_any_device: bool = False) -> CommandResult:
    """Export a block device. The device is validated first; see the module docstring."""
    validate_device(device_path, allow_any=allow_any_device)
    return run(['/backstores/block', 'create', f'name={name}', f'dev={device_path}'])


def create_pscsi_backstore(device_path: str, name: str, *,
                           allow_any_device: bool = False) -> CommandResult:
    """Export a SCSI device pass-through - how a tape drive is exported."""
    validate_device(device_path, allow_any=allow_any_device)
    return run(['/backstores/pscsi', 'create', f'name={name}', f'dev={device_path}'])


def delete_backstore(plugin: str, name: str) -> CommandResult:
    return run([f'/backstores/{plugin}', 'delete', name])


def create_target(iqn: str) -> CommandResult:
    validate_iqn(iqn)
    return run(['/iscsi', 'create', iqn])


def delete_target(iqn: str) -> CommandResult:
    validate_iqn(iqn)
    return run(['/iscsi', 'delete', iqn])


def create_lun(iqn: str, plugin: str, name: str, tpg: int = 1,
               lun: int = None) -> CommandResult:
    """Map a backstore into a target; targetcli picks the number unless `lun`
    names one. A changer goes at LUN 0: some backup software stops scanning
    at the first tape drive it meets."""
    validate_iqn(iqn)
    args = [f'/iscsi/{iqn}/tpg{tpg}/luns', 'create', f'/backstores/{plugin}/{name}']
    if lun is not None:
        args.append(f'lun={int(lun)}')
    return run(args)


def delete_lun(iqn: str, lun_id: int, tpg: int = 1) -> CommandResult:
    validate_iqn(iqn)
    return run([f'/iscsi/{iqn}/tpg{tpg}/luns', 'delete', f'lun{lun_id}'])


def create_acl(iqn: str, initiator_iqn: str, tpg: int = 1) -> CommandResult:
    """Allow one initiator to see this target."""
    validate_iqn(iqn)
    validate_iqn(initiator_iqn)
    return run([f'/iscsi/{iqn}/tpg{tpg}/acls', 'create', initiator_iqn])


def delete_acl(iqn: str, initiator_iqn: str, tpg: int = 1) -> CommandResult:
    validate_iqn(iqn)
    validate_iqn(initiator_iqn)
    return run([f'/iscsi/{iqn}/tpg{tpg}/acls', 'delete', initiator_iqn])


def create_portal(iqn: str, ip: str = '0.0.0.0', port: int = 3260,
                  tpg: int = 1) -> CommandResult:
    validate_iqn(iqn)
    return run([f'/iscsi/{iqn}/tpg{tpg}/portals', 'create', ip, str(port)])


def delete_portal(iqn: str, ip: str = '0.0.0.0', port: int = 3260,
                  tpg: int = 1) -> CommandResult:
    """Stop listening for a target on one address.

    Deleting the last portal leaves a target nothing can connect to, which
    looks from the initiator side exactly like the target being gone.
    """
    validate_iqn(iqn)
    return run([f'/iscsi/{iqn}/tpg{int(tpg)}/portals', 'delete', str(ip),
                str(int(port))])


def set_auth(iqn: str, *, initiator: str = None, userid: str = '', password: str = '',
             mutual_userid: str = '', mutual_password: str = '',
             tpg: int = 1) -> CommandResult:
    """Set, or with empty values clear, CHAP on one ACL or on the whole TPG.

    On an ACL it applies to that initiator; on the TPG it applies to the
    initiators that come in without an ACL of their own (generate_node_acls).
    The values are checked unless every one is empty (clearing).
    """
    validate_iqn(iqn)
    if initiator:
        validate_iqn(initiator)
    if any((userid, password, mutual_userid, mutual_password)):
        validate_chap(userid, password, mutual_userid, mutual_password)
    path = f'/iscsi/{iqn}/tpg{int(tpg)}' + (f'/acls/{initiator}' if initiator else '')
    # An empty value has to be written as '' - targetcli reads a bare
    # "userid= password=" as userid set to the text "password=", so clearing
    # that way set the credentials to garbage instead of removing them.
    values = (('userid', userid), ('password', password),
              ('mutual_userid', mutual_userid), ('mutual_password', mutual_password))
    return run([path, 'set', 'auth',
                *(f'{name}={value}' if value else f"{name}=''" for name, value in values)])


def set_attribute(iqn: str, attribute: str, value: str, tpg: int = 1) -> CommandResult:
    """Set a TPG attribute, such as generate_node_acls."""
    validate_iqn(iqn)
    return run([f'/iscsi/{iqn}/tpg{tpg}', 'set', 'attribute', f'{attribute}={value}'])
