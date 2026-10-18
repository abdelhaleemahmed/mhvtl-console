"""Moving tapes around: mtx, mt and vtlcmd.

Modules:
    mtx.py       the robot: status, load, unload, transfer, inventory
    mt.py        the drive: density, position, medium present
    vtlcmd.py    the message queue: online, offline, MAP, stats
    logsense.py  the volume statistics log page: the same counters, upstream
    elements.py  READ ELEMENT STATUS: which tape is in which drive
    service.py   OperationsService: mount, unmount, move, online, offline

Two rules this package exists to enforce: the device is resolved by SCSI address
rather than by position, and vtlcmd is addressed by the device.conf id rather
than a derived index.
"""
from .mt import DriveStatus
from .mtx import Element, LibraryStatus
from .service import OperationsService
from .vtlcmd import TapeStats

__all__ = ['OperationsService', 'LibraryStatus', 'Element', 'DriveStatus', 'TapeStats']
