"""SCSI dataclasses.

Collapses the two rival ScsiDevice definitions - tape_operations_service.py:130
and console_service.py:112 - into one. They disagreed about whether the SCSI
address was a single string or four fields; it is both here, parsed once.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

HCTL_RE = re.compile(r'^\[?(\d+):(\d+):(\d+):(\d+)\]?$')

#: lsscsi's type column for the devices MHVTL creates.
CHANGER = 'mediumx'
TAPE = 'tape'


@dataclass(frozen=True)
class ScsiAddress:
    """host:channel:target:lun, as lsscsi prints it in brackets."""
    host: int
    channel: int
    target: int
    lun: int

    @classmethod
    def parse(cls, text: str) -> Optional['ScsiAddress']:
        match = HCTL_RE.match((text or '').strip())
        if not match:
            return None
        return cls(*(int(part) for part in match.groups()))

    @property
    def ctl(self) -> Tuple[int, int, int]:
        """(channel, target, lun) - what device.conf records.

        device.conf does not name the SCSI host, because the host number changes
        every time the module is reloaded. Matching on these three is what makes
        a mapping survive a restart.
        """
        return (self.channel, self.target, self.lun)

    def __str__(self) -> str:
        return f'[{self.host}:{self.channel}:{self.target}:{self.lun}]'


@dataclass
class ScsiDevice:
    """One line of lsscsi output."""
    address: Optional[ScsiAddress]
    device_type: str
    vendor: str
    model: str
    revision: str
    device_path: str = ''          # /dev/st0, /dev/sch1
    generic_path: str = ''         # /dev/sg4

    @property
    def is_changer(self) -> bool:
        return self.device_type == CHANGER

    @property
    def is_tape(self) -> bool:
        return self.device_type == TAPE

    @property
    def preferred_path(self) -> str:
        """The node to hand to mtx: the generic one when there is one.

        mtx talks to /dev/sg*; mt talks to /dev/st*. Callers that need the other
        one ask for it explicitly.
        """
        return self.generic_path or self.device_path

    def to_dict(self) -> Dict:
        return {
            'host': str(self.address) if self.address else '',
            'address': list(self.address.ctl) if self.address else None,
            'device_type': self.device_type,
            'vendor': self.vendor,
            'model': self.model,
            'revision': self.revision,
            'device_path': self.device_path,
            'generic_path': self.generic_path,
        }
