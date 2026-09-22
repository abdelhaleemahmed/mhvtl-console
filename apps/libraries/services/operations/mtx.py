"""The mtx wrapper and its output parser.

Moved from tape_operations_service.py - the status parser at :964, and the
command calls at :846 (status), :1163 (load), :1229 (unload), :1295 (transfer)
and :1762 (inventory).

Parsing is a pure function over mtx's text, so it is tested from captured
fixtures rather than by moving real tapes around. Running mtx is separate, and
goes through core.retry: the robot reports "Logical Unit Not Ready" for a second
or two after a move, and a caller that gives up at the first failure turns a
working library into an intermittent one.

mtx output looks like this::

      Storage Changer /dev/sg4:4 Drives, 43 Slots ( 4 Import/Export )
    Data Transfer Element 0:Full (Storage Element 3 Loaded):VolumeTag = E01003L8
    Data Transfer Element 1:Empty
          Storage Element 1:Full :VolumeTag=E01001L8
          Storage Element 40 IMPORT/EXPORT:Empty

Note that a tape loaded into a drive still occupies its home slot in the listing,
which is why a drive line names the Storage Element it came from.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core import NORMAL, CommandResult, retry_on_busy, shell

logger = logging.getLogger(__name__)

HEADER_RE = re.compile(
    r'Storage Changer\s+(\S+):(\d+)\s+Drives,\s+(\d+)\s+Slots\s*(?:\(\s*(\d+)\s+Import/Export\s*\))?')
DRIVE_RE = re.compile(
    r'Data Transfer Element\s+(\d+):(Full|Empty)'
    r'(?:\s*\(Storage Element\s+(\d+)\s+Loaded\))?'
    r'(?::VolumeTag\s*=\s*(\S+))?')
SLOT_RE = re.compile(
    r'Storage Element\s+(\d+)(\s+IMPORT/EXPORT)?:(Full|Empty)'
    r'(?:\s*:VolumeTag\s*=\s*(\S+))?')


@dataclass
class Element:
    """A slot or a drive, as mtx reports it."""
    number: int
    full: bool
    barcode: Optional[str] = None
    slot_origin: Optional[int] = None      # drives only: where the tape came from
    import_export: bool = False

    def to_dict(self) -> Dict:
        data = {'number': self.number, 'full': self.full, 'barcode': self.barcode}
        if self.slot_origin is not None:
            data['slot_origin'] = self.slot_origin
        if self.import_export:
            data['import_export'] = True
        return data


@dataclass
class LibraryStatus:
    """Everything one `mtx status` says."""
    device: str = ''
    drives: List[Element] = field(default_factory=list)
    slots: List[Element] = field(default_factory=list)
    import_export: List[Element] = field(default_factory=list)
    declared_drives: Optional[int] = None
    declared_slots: Optional[int] = None
    raw: str = ''

    @property
    def summary(self) -> Dict:
        return {
            'total_slots': len(self.slots),
            'full_slots': sum(1 for s in self.slots if s.full),
            'empty_slots': sum(1 for s in self.slots if not s.full),
            'total_drives': len(self.drives),
            'loaded_drives': sum(1 for d in self.drives if d.full),
            'ie_slots': len(self.import_export),
        }

    def slot(self, number: int) -> Optional[Element]:
        return next((s for s in self.slots if s.number == number), None)

    def drive(self, number: int) -> Optional[Element]:
        return next((d for d in self.drives if d.number == number), None)

    def find_barcode(self, barcode: str) -> Optional[Element]:
        """Where a tape is, slot or drive."""
        for element in list(self.slots) + list(self.drives):
            if element.barcode == barcode:
                return element
        return None

    def to_dict(self) -> Dict:
        return {
            'device': self.device,
            'drives': [d.to_dict() for d in self.drives],
            'slots': [s.to_dict() for s in self.slots],
            'import_export': [s.to_dict() for s in self.import_export],
            'summary': self.summary,
        }


def parse(text: str) -> LibraryStatus:
    """Parse `mtx status` output. Never raises; unknown lines are ignored."""
    status = LibraryStatus(raw=text)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        header = HEADER_RE.search(line)
        if header:
            status.device = header.group(1)
            status.declared_drives = int(header.group(2))
            status.declared_slots = int(header.group(3))
            continue

        drive = DRIVE_RE.match(line)
        if drive:
            status.drives.append(Element(
                number=int(drive.group(1)),
                full=drive.group(2) == 'Full',
                slot_origin=int(drive.group(3)) if drive.group(3) else None,
                barcode=(drive.group(4) or '').strip() or None))
            continue

        slot = SLOT_RE.match(line)
        if slot:
            element = Element(
                number=int(slot.group(1)),
                full=slot.group(3) == 'Full',
                barcode=(slot.group(4) or '').strip() or None,
                import_export=bool(slot.group(2)))
            (status.import_export if element.import_export else status.slots).append(element)

    return status


# -- running mtx -----------------------------------------------------------

def _run(device: str, *args: str, timeout: int = NORMAL,
         description: str = 'mtx') -> CommandResult:
    argv = ['mtx', '-f', device, *args]
    return retry_on_busy(lambda: shell.sudo(argv, timeout=timeout),
                         description=description)


def status(device: str) -> LibraryStatus:
    """Read a library's slot map. Returns an empty status if mtx fails."""
    result = _run(device, 'status', description=f'mtx status {device}')
    if not result.ok:
        logger.warning('mtx status on %s: %s', device, result.output.strip()[:200])
        return LibraryStatus(device=device, raw=result.output)
    return parse(result.stdout)


def load(device: str, slot: int, drive: int) -> CommandResult:
    """Move a tape from a storage slot into a drive."""
    return _run(device, 'load', str(slot), str(drive),
                description=f'mtx load {slot} -> drive {drive}')


def unload(device: str, slot: int, drive: int) -> CommandResult:
    """Move a tape from a drive back to a storage slot."""
    return _run(device, 'unload', str(slot), str(drive),
                description=f'mtx unload drive {drive} -> {slot}')


def transfer(device: str, from_slot: int, to_slot: int) -> CommandResult:
    """Move a tape between two storage slots."""
    return _run(device, 'transfer', str(from_slot), str(to_slot),
                description=f'mtx transfer {from_slot} -> {to_slot}')


def inventory(device: str) -> CommandResult:
    """Ask the robot to re-read its barcodes."""
    return _run(device, 'inventory', timeout=NORMAL * 2,
                description=f'mtx inventory {device}')
