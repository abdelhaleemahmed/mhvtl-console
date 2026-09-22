"""Drive dataclasses.

Collapses the two rival DriveInfo definitions (tape_operations_service.py:268 and
backup_mhvtl_library_service.py) into one.

A drive's identity in MHVTL is its queue id - the number in `Drive: 11` in
device.conf, which is also the argument to vtlcmd and the systemd instance name
(vtltape@11.service). Its SCSI address (channel, target, lun) is what maps it to
a /dev node; its slot is its position within the library.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class DriveInfo:
    """One drive, as device.conf describes it."""
    drive_id: int
    library_id: Optional[int] = None
    slot: Optional[int] = None
    vendor: str = ''
    product: str = ''
    serial: str = ''
    channel: Optional[int] = None
    target: Optional[int] = None
    lun: Optional[int] = None
    naa: str = ''
    compression: str = ''

    @property
    def address(self):
        """(channel, target, lun) - what a /dev node is matched on.

        Never match a drive to a device by position in a list: sg numbers are
        reassigned when the module reloads.
        """
        if None in (self.channel, self.target, self.lun):
            return None
        return (self.channel, self.target, self.lun)

    @property
    def unit_name(self) -> str:
        """The systemd unit that runs this drive's daemon."""
        return f'vtltape@{self.drive_id}.service'

    def to_dict(self) -> Dict:
        return {
            'drive_id': self.drive_id,
            'library_id': self.library_id,
            'slot': self.slot,
            'vendor': self.vendor,
            'product': self.product,
            'serial': self.serial,
            'channel': self.channel,
            'target': self.target,
            'lun': self.lun,
            'naa': self.naa,
            'compression': self.compression,
        }

    @classmethod
    def from_config(cls, drive_id, data: Dict) -> 'DriveInfo':
        """Build from one entry of the parsed device.conf."""
        return cls(
            drive_id=int(drive_id),
            library_id=data.get('library_id'),
            slot=data.get('slot'),
            vendor=data.get('vendor', ''),
            product=data.get('product', ''),
            serial=data.get('serial', ''),
            channel=data.get('channel'),
            target=data.get('target'),
            lun=data.get('lun'),
            naa=data.get('naa', ''),
            compression=data.get('compression', ''),
        )
