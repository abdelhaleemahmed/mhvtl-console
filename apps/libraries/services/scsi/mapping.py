"""Which /dev node belongs to which library or drive.

Moved from tape_operations_service.py:520-600, written while fixing the move-tape
page, and the rule it encodes is the important part of this module:

    Match on the CHANNEL/TARGET/LUN that device.conf assigns. Never by position.

The original paired the Nth `Library:` line with the Nth changer from lsscsi.
That is correct only while the two orders agree, and they agree by luck:

    - device.conf order is whatever wrote the file. On this host it is 10, 30,
      20, because the libraries were created out of order;
    - lsscsi orders by SCSI address;
    - the sg numbers themselves are reassigned on every module reload. During
      one session here /dev/sg3 was library 20's changer in the morning and
      library 30's in the afternoon.

Get it wrong and a move or an unmount acts on a different library than the one
the operator chose, which is why this returns None rather than guessing when no
device reports the expected address.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Dict, List, Optional, Tuple

from ..config import device_conf as device_conf_format
from ..core import shell
from ..core import device_conf_path
from . import lsscsi
from .models import ScsiDevice

logger = logging.getLogger(__name__)


def _load_conf(config_dir=None) -> Optional[device_conf_format.DeviceConf]:
    result = shell.sudo_cat(device_conf_path(config_dir))
    if not result.ok:
        logger.warning('cannot read device.conf: %s', result.stderr.strip()[:200])
        return None
    return device_conf_format.parse(result.stdout)


def _find(devices: List[ScsiDevice], address: Tuple[int, int, int],
          want_changer: bool) -> Optional[ScsiDevice]:
    for device in devices:
        if device.address is None or device.address.ctl != address:
            continue
        if want_changer and device.is_changer:
            return device
        if not want_changer and device.is_tape:
            return device
    return None


def device_for_library(library_id: int, *, devices: List[ScsiDevice] = None,
                       config_dir=None) -> Optional[str]:
    """The changer node for a library, or None if it cannot be identified."""
    conf = _load_conf(config_dir)
    if conf is None:
        return None

    address = conf.address_of(library_id) if library_id in conf.libraries else None
    if address is None:
        logger.warning('library %s has no entry in device.conf', library_id)
        return None

    device = _find(devices if devices is not None else lsscsi.local(),
                   address, want_changer=True)
    if device is None:
        logger.warning('library %s is at channel/target/lun %s in device.conf, '
                       'but no changer reports that address; not guessing',
                       library_id, address)
        return None
    return changer_node(device)


def changer_node(device: ScsiDevice) -> Optional[str]:
    """The node a changer is driven through, as MHVTL_CHANGER_NODE says.

    'generic' - the default - is /dev/sgN, which mtx and everything else here
    use. 'ch' is /dev/schN from the kernel ch driver, used when the driver is
    loaded and bound; otherwise this falls back to the generic node rather than
    returning one that does not exist. See scsi/ch_policy.py for why ch is off.
    """
    if node_preference() == 'ch' and device.device_path and \
            device.device_path.startswith('/dev/sch'):
        return device.device_path
    return device.generic_path or device.device_path


def node_preference() -> str:
    try:
        from django.conf import settings
        wanted = str(getattr(settings, 'MHVTL_CHANGER_NODE', 'generic')).lower()
    except Exception:                                  # noqa: BLE001 - no Django
        wanted = 'generic'
    return 'ch' if wanted in ('ch', 'sch') else 'generic'


def device_for_drive(drive_id: int, *, devices: List[ScsiDevice] = None,
                     config_dir=None, generic: bool = False) -> Optional[str]:
    """The tape node for a drive.

    mt wants /dev/st*, which is the default here; pass generic=True for the
    /dev/sg* node that SCSI-level tools use.
    """
    conf = _load_conf(config_dir)
    if conf is None:
        return None

    address = conf.address_of(drive_id) if drive_id in conf.drives else None
    if address is None:
        logger.warning('drive %s has no entry in device.conf', drive_id)
        return None

    device = _find(devices if devices is not None else lsscsi.local(),
                   address, want_changer=False)
    if device is None:
        logger.warning('drive %s is at channel/target/lun %s in device.conf, but '
                       'no tape device reports that address; not guessing',
                       drive_id, address)
        return None
    return device.generic_path if generic else (device.device_path or device.generic_path)


def map_all(config_dir=None) -> Dict[str, Dict[int, Optional[str]]]:
    """Every library and drive with the node it resolves to.

    Useful for a CLI `scsi map`, and for spotting a library whose daemon has not
    started: its address is in device.conf but no device reports it.
    """
    conf = _load_conf(config_dir)
    if conf is None:
        return {'libraries': {}, 'drives': {}}

    devices = lsscsi.local()
    return {
        'libraries': {library_id: device_for_library(library_id, devices=devices,
                                                     config_dir=config_dir)
                      for library_id in conf.libraries},
        'drives': {drive_id: device_for_drive(drive_id, devices=devices,
                                              config_dir=config_dir)
                   for drive_id in conf.drives},
    }
