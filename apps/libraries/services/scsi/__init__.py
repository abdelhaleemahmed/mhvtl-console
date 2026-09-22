"""SCSI discovery, and mapping a library or drive to its device node.

Modules:
    models.py    ScsiDevice, ScsiAddress
    lsscsi.py    parsing and running lsscsi
    mapping.py   device.conf address -> /dev node

The rule worth remembering: a library or drive is matched to its device by the
CHANNEL/TARGET/LUN device.conf gives it, never by position in a list. sg numbers
are reassigned whenever the module reloads.
"""
from .lsscsi import changers, discover, parse, parse_line, tapes
from .mapping import device_for_drive, device_for_library, map_all
from .models import ScsiAddress, ScsiDevice

__all__ = ['ScsiDevice', 'ScsiAddress', 'discover', 'parse', 'parse_line',
           'changers', 'tapes', 'device_for_library', 'device_for_drive', 'map_all']
