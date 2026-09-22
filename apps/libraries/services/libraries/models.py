"""Library dataclasses.

Collapses the three rival LibraryInfo definitions - service_results.py:110,
mhvtl_library_service.py and backup_mhvtl_library_service.py - into one.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class LibraryInfo:
    """One library, as device.conf describes it."""
    library_id: int
    vendor: str = ''
    product: str = ''
    serial: str = ''
    channel: Optional[int] = None
    target: Optional[int] = None
    lun: Optional[int] = None
    naa: str = ''
    home_directory: str = ''
    drive_ids: List[int] = field(default_factory=list)
    #: Filled in by the service when it has read library_contents.
    slot_count: Optional[int] = None
    tape_count: Optional[int] = None

    @property
    def address(self) -> Optional[Tuple[int, int, int]]:
        """(channel, target, lun) - how the changer is found among SCSI devices."""
        if None in (self.channel, self.target, self.lun):
            return None
        return (self.channel, self.target, self.lun)

    @property
    def drive_count(self) -> int:
        return len(self.drive_ids)

    @property
    def unit_name(self) -> str:
        """The systemd unit running this library's robot daemon."""
        return f'vtllibrary@{self.library_id}.service'

    @property
    def model(self) -> str:
        return f'{self.vendor} {self.product}'.strip()

    def to_dict(self) -> Dict:
        return {
            'library_id': self.library_id,
            'vendor': self.vendor,
            'product': self.product,
            'model': self.model,
            'serial': self.serial,
            'channel': self.channel,
            'target': self.target,
            'lun': self.lun,
            'naa': self.naa,
            'home_directory': self.home_directory,
            'drives': self.drive_count,
            'drive_ids': self.drive_ids,
            'slot_count': self.slot_count,
            'tape_count': self.tape_count,
        }

    @classmethod
    def from_config(cls, library_id, data: Dict, drive_ids: List[int] = None):
        return cls(
            library_id=int(library_id),
            vendor=data.get('vendor', ''),
            product=data.get('product', ''),
            serial=data.get('serial', ''),
            channel=data.get('channel'),
            target=data.get('target'),
            lun=data.get('lun'),
            naa=data.get('naa', ''),
            home_directory=data.get('home_directory', ''),
            drive_ids=sorted(drive_ids or []),
        )
