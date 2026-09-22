"""Read and write library_contents.N - a library's slot inventory.

Moved from tape_operations_service.py:2398 (_parse_library_contents), the
richest of the three implementations. It replaces
mhvtl_library_service.py:528 (_get_barcodes_from_library_contents), a
startswith-and-swallow-exceptions variant.

One deliberate change: this parses, and only parses. The original called
_get_tape_size_mb() and _get_tape_used_mb() for every tape, each shelling out
through sudo at roughly 69ms, so listing library 10's 32 tapes cost 64 sudo
calls - which the operator dashboard paid on every page load. Size and used are
now a separate, optional step (see tapes/media.py), so a caller that only wants
to know what is in which slot pays nothing.

Format:

    VERSION: 2

    Drive 1:                  <- the drive's serial, when set. NOT a barcode.
    Drive 2:

    Picker 1:

    MAP 1:

    Slot 1: E01001L8
    Slot 2: E01002L8
    Slot 3:                   <- empty slot

The value after `Drive N:` is the drive's serial number, which
init_drive_slot() copies into inq_product_sno. Reading it as a tape barcode
invents media that does not exist - the mistake that made our upstream
tape-reload patch unsubmittable.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SLOT_RE = re.compile(r'^\s*Slot\s+(\d+):\s*(\S+)?\s*$')
DRIVE_RE = re.compile(r'^\s*Drive\s+(\d+):\s*(\S+)?\s*$')
MAP_RE = re.compile(r'^\s*MAP\s+(\d+):\s*(\S+)?\s*$')
PICKER_RE = re.compile(r'^\s*Picker\s+(\d+):\s*(\S+)?\s*$')
VERSION_RE = re.compile(r'^\s*VERSION:\s*(\d+)\s*$')

#: Barcode suffix to media density: the one table in profiles/personalities,
#: taken from MHVTL's make_vtl_media. This module kept its own copy, which
#: stopped at AIT4 and T10K and so reported no density for most non-LTO tapes.
from ..profiles.personalities import DENSITY_BY_SUFFIX  # noqa: E402

CLEANING_PREFIX = 'CLN'
WORM_PREFIX = 'W'


@dataclass
class Slot:
    """One storage slot, empty or holding a tape."""
    number: int
    barcode: Optional[str] = None

    @property
    def full(self) -> bool:
        return bool(self.barcode)

    @property
    def kind(self) -> str:
        """data, clean or WORM, decided by the barcode's leading characters."""
        if not self.barcode:
            return 'empty'
        if self.barcode.startswith(CLEANING_PREFIX):
            return 'clean'
        if self.barcode.startswith(WORM_PREFIX):
            return 'WORM'
        return 'data'

    @property
    def density(self) -> Optional[str]:
        """Media density from the barcode's two-character suffix."""
        if not self.barcode or len(self.barcode) < 2:
            return None
        return DENSITY_BY_SUFFIX.get(self.barcode[-2:].upper())

    def to_dict(self) -> Dict:
        return {'slot': self.number, 'barcode': self.barcode, 'full': self.full,
                'kind': self.kind, 'density': self.density}


@dataclass
class LibraryContents:
    """What library_contents.N declares."""
    version: Optional[int] = None
    slots: List[Slot] = field(default_factory=list)
    map_slots: List[Slot] = field(default_factory=list)
    #: drive slot number -> serial number (not a barcode; see the module docstring)
    drive_serials: Dict[int, Optional[str]] = field(default_factory=dict)
    pickers: Dict[int, Optional[str]] = field(default_factory=dict)

    @property
    def barcodes(self) -> List[str]:
        return [slot.barcode for slot in self.slots if slot.barcode]

    @property
    def occupied(self) -> List[Slot]:
        return [slot for slot in self.slots if slot.full]

    @property
    def drive_count(self) -> int:
        return len(self.drive_serials)

    def summary(self) -> Dict:
        occupied = self.occupied
        return {
            'total_slots': len(self.slots),
            'full_slots': len(occupied),
            'empty_slots': len(self.slots) - len(occupied),
            'map_slots': len(self.map_slots),
            'drives': self.drive_count,
            'cleaning_tapes': sum(1 for slot in occupied if slot.kind == 'clean'),
        }

    def slot_stats(self) -> Dict:
        """total/occupied/free and the lowest empty slot (None when full), the
        figures the library detail page shows."""
        occupied = len(self.occupied)
        free_slot = next((slot.number for slot in sorted(self.slots, key=lambda s: s.number)
                          if not slot.full), None)
        return {'total': len(self.slots), 'occupied': occupied,
                'free': len(self.slots) - occupied, 'next_slot': free_slot}

    def to_dict(self) -> Dict:
        return {
            'version': self.version,
            'slots': [slot.to_dict() for slot in self.slots],
            'map_slots': [slot.to_dict() for slot in self.map_slots],
            'drive_serials': self.drive_serials,
            'summary': self.summary(),
        }


def parse(text: str) -> LibraryContents:
    """Parse library_contents text. Never raises; unknown lines are ignored."""
    contents = LibraryContents()

    for raw in text.splitlines():
        line = raw.split('#', 1)[0] if raw.lstrip().startswith('#') else raw
        if not line.strip():
            continue

        match = VERSION_RE.match(line)
        if match:
            contents.version = int(match.group(1))
            continue

        match = SLOT_RE.match(line)
        if match:
            contents.slots.append(Slot(int(match.group(1)), match.group(2)))
            continue

        match = DRIVE_RE.match(line)
        if match:
            contents.drive_serials[int(match.group(1))] = match.group(2)
            continue

        match = MAP_RE.match(line)
        if match:
            contents.map_slots.append(Slot(int(match.group(1)), match.group(2)))
            continue

        match = PICKER_RE.match(line)
        if match:
            contents.pickers[int(match.group(1))] = match.group(2)

    return contents


def render(contents: LibraryContents) -> str:
    """Write library_contents text, in the layout MHVTL's generator uses."""
    lines = [f'VERSION: {contents.version or 2}', '']

    for number in sorted(contents.drive_serials):
        serial = contents.drive_serials[number] or ''
        lines.append(f'Drive {number}:{" " + serial if serial else ""}')
    lines.append('')

    for number in sorted(contents.pickers):
        value = contents.pickers[number] or ''
        lines.append(f'Picker {number}:{" " + value if value else ""}')
    lines.append('')

    for slot in sorted(contents.map_slots, key=lambda s: s.number):
        lines.append(f'MAP {slot.number}:{" " + slot.barcode if slot.barcode else ""}')
    lines.append('')

    for slot in sorted(contents.slots, key=lambda s: s.number):
        lines.append(f'Slot {slot.number}:{" " + slot.barcode if slot.barcode else ""}')

    return '\n'.join(lines) + '\n'


def add_drive_slot(text: str, slot: int) -> str:
    """Insert `Drive N:` at the end of the existing drive block."""
    lines = text.splitlines(keepends=True)
    if any(re.match(rf'^\s*Drive\s+{slot}\s*:', line) for line in lines):
        return text

    last = max((i for i, line in enumerate(lines)
                if line.strip().startswith('Drive ')), default=None)
    entry = f'Drive {slot}:\n'
    lines.insert(0 if last is None else last + 1, entry)
    return ''.join(lines)


def remove_drive_slot(text: str, slot: int) -> str:
    """Drop one `Drive N:` line."""
    return ''.join(line for line in text.splitlines(keepends=True)
                   if not re.match(rf'^\s*Drive\s+{slot}\s*:', line))


#: The barcode legend MHVTL's own make_vtl_media writes into every generated
#: library_contents file. Kept verbatim: an operator editing the file by hand
#: reads the suffix table here, and a file without it looks unlike every other
#: one on the system.
BARCODE_LEGEND = """\
# Slot 1 - ?, no gaps
# Slot N: [barcode]
# [barcode]
# a barcode is comprised of three fields: [Leading] [identifier] [Trailing]
# Leading "CLN" -- cleaning tape
# Leading "W" -- WORM tape
# Leading "NOBAR" -- will appear to have no barcode
# If the barcode is at least 8 character long, then the last two characters are Trailing
# Trailing "S3" - SDLT600
# Trailing "X4" - AIT-4
# Trailing "L1" - LTO 1, "L2" - LTO 2, "L3" - LTO 3, "L4" - LTO 4, "L5" - LTO 5
# Trailing "LT" - LTO 3 WORM, "LU" -  LTO 4 WORM, "LV" - LTO 5 WORM
# Trailing "L6" - LTO 6, "LW" - LTO 6 WORM
# Trailing "TA" - T10000+
# Trailing "TZ" - 9840A, "TY" - 9840B, "TX" - 9840C, "TW" - 9840D
# Trailing "TV" - 9940A, "TU" - 9940B
# Trailing "JA" - 3592+
# Trailing "JB" - 3592E05+
# Trailing "JC" - 3592E06+
# Trailing "JK" - 3592E07+
# Trailing "JW" - WORM 3592+
# Trailing "JX" - WORM 3592E05+ & 3592E06
# Trailing "JY" - WORM 3592E07+
# Trailing "D7" - DLT7000 media (DLT IV)
#
"""


def render_new(library_id: int, drive_count: int, *, barcode_prefix: str = None,
               media_suffix: str = 'L8', media_count: int = 39,
               empty_slots: int = 0, map_count: int = 4) -> str:
    """The library_contents file for a library that does not exist yet.

    Moved from mhvtl_library_service.py:_generate_library_contents_rpm_text.
    Separate from render() because the two have different inputs: render() takes
    a parsed file and writes it back, this takes a specification and a count.

    Barcodes are prefix + a three-digit number + suffix, which is what
    tapes/barcodes builds and what MHVTL parses the density out of, so a tape
    added to this library later continues the same series.

    Slots are numbered from 1 with no gaps - MHVTL stops reading at the first
    missing slot number, so a gap silently shortens the library.
    """
    prefix = (barcode_prefix or f'M{int(library_id):02d}').upper()[:3]
    suffix = (media_suffix or 'L8').upper()

    lines = ['VERSION: 2', '']
    lines += [f'Drive {index}:' for index in range(1, int(drive_count) + 1)]
    lines += ['', 'Picker 1:', '']
    lines += [f'MAP {index}:' for index in range(1, int(map_count) + 1)]
    lines += ['', BARCODE_LEGEND.rstrip('\n')]

    slot = 1
    for number in range(1, int(media_count) + 1):
        lines.append(f'Slot {slot}: {prefix}{number:03d}{suffix}')
        slot += 1
    for _ in range(int(empty_slots)):
        lines.append(f'Slot {slot}:')
        slot += 1

    return '\n'.join(lines) + '\n'
