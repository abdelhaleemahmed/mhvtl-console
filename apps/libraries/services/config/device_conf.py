"""Read and write /etc/mhvtl/device.conf.

Moved from mhvtl_library_service.py:1260 (_parse_device_conf_rpm), the best of
the four implementations in the tree because it takes text rather than a path -
no file I/O, no sudo, so it can be tested against captured fixtures. The rivals
it replaces::

    backup_mhvtl_library_service.py:808   parsing welded to its own sudo fallback
    mhvtl_script_service.py:187           regex scan with a plain open(), which
                                          fails on a root-owned file
    tape_operations_service.py:536,591,613  three ad-hoc scans, each re-reading
                                          the file for one field

The rendering half (render_drive, render_library) is new only in the sense that
it was previously an f-string inside add_drive; it lives here so that the file's
format is described in one place.

Format, for reference:

    Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00
     Vendor identification: STK
     Product identification: L700
     Unit serial number: XYZZY_A
     NAA: 10:22:33:44:ab:00:00:00
     Home directory: /opt/mhvtl

    Drive: 11 CHANNEL: 00 TARGET: 01 LUN: 00
     Library ID: 10 Slot: 01
     ...

Records are separated by blank lines. Ids are decimal; CHANNEL/TARGET/LUN are
zero-padded but parse as plain integers.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

LIBRARY_RE = re.compile(
    r'^Library:\s+(\d+)\s+CHANNEL:\s+(\d+)\s+TARGET:\s+(\d+)\s+LUN:\s+(\d+)\s*$')
DRIVE_RE = re.compile(
    r'^Drive:\s+(\d+)\s+CHANNEL:\s+(\d+)\s+TARGET:\s+(\d+)\s+LUN:\s+(\d+)\s*$')
LIBRARY_ID_SLOT_RE = re.compile(r'^\s*Library ID:\s+(\d+)\s+Slot:\s+(\d+)\s*$')

#: Indented ` Key: value` lines, mapped to the field they set.
FIELDS = {
    'Vendor identification': 'vendor',
    'Product identification': 'product',
    'Unit serial number': 'serial',
    'NAA': 'naa',
    'Home directory': 'home_directory',
    'Compression': 'compression',
    'Backoff': 'backoff',
}

#: The highest SCSI target this application hands out.
#:
#: Not an MHVTL limit - MHVTL has none: the daemons parse TARGET unchecked
#: (usr/cmd/vtllibrary.c:1226, usr/cmd/vtltape.c:2124) and units are added with
#: __scsi_add_device (kernel/mhvtl.c:1157), which ignores the host's max_id.
#: The cap is 99 because the target is written in decimal into the NAA line,
#: which MHVTL reads as two-hex-digit fields (usr/cmd/vtllibrary.c:1286); a
#: three-digit target makes it discard the configured NAA. The previous value,
#: 50, came from the old fork's allocator with no source.
#: See docs/sphinx/guides/library-limits.rst.
MAX_TARGET = 99


@dataclass
class DeviceConf:
    """Everything device.conf declares.

    libraries and drives are keyed by id and keep the order they appear in the
    file - which is *not* sorted and *not* SCSI order. Never pair these with
    lsscsi output by position; match on address().
    """
    libraries: Dict[int, Dict] = field(default_factory=dict)
    drives: Dict[int, Dict] = field(default_factory=dict)

    @property
    def drive_counts(self) -> Dict[int, int]:
        """How many drives each library has."""
        counts: Dict[int, int] = {}
        for drive in self.drives.values():
            library_id = drive.get('library_id')
            if library_id is not None:
                counts[library_id] = counts.get(library_id, 0) + 1
        return counts

    def drives_of(self, library_id: int) -> Dict[int, Dict]:
        return {drive_id: data for drive_id, data in self.drives.items()
                if data.get('library_id') == library_id}

    def address_of(self, device_id: int) -> Optional[Tuple[int, int, int]]:
        """(channel, target, lun) for a library or drive id."""
        entry = self.libraries.get(device_id) or self.drives.get(device_id)
        if not entry:
            return None
        address = (entry.get('channel'), entry.get('target'), entry.get('lun'))
        return None if None in address else address

    def used_targets(self) -> set:
        """Every SCSI target already claimed, by a library or a drive."""
        targets = {entry.get('target') for entry in self.libraries.values()}
        targets |= {entry.get('target') for entry in self.drives.values()}
        targets.discard(None)
        return targets

    def next_free_target(self) -> Optional[int]:
        used = self.used_targets()
        return next((t for t in range(MAX_TARGET + 1) if t not in used), None)

    def to_dict(self) -> Dict:
        return {
            'libraries': self.libraries,
            'drives': self.drives,
            'drive_counts': self.drive_counts,
        }


def parse(text: str) -> DeviceConf:
    """Parse device.conf text. Never raises: unknown lines are ignored."""
    conf = DeviceConf()
    current: Optional[Dict] = None
    kind: Optional[str] = None

    for line in text.splitlines():
        match = LIBRARY_RE.match(line)
        if match:
            library_id = int(match.group(1))
            current = conf.libraries[library_id] = {
                'channel': int(match.group(2)),
                'target': int(match.group(3)),
                'lun': int(match.group(4)),
            }
            kind = 'library'
            continue

        match = DRIVE_RE.match(line)
        if match:
            drive_id = int(match.group(1))
            current = conf.drives[drive_id] = {
                'channel': int(match.group(2)),
                'target': int(match.group(3)),
                'lun': int(match.group(4)),
            }
            kind = 'drive'
            continue

        if current is None:
            continue

        if kind == 'drive':
            match = LIBRARY_ID_SLOT_RE.match(line)
            if match:
                current['library_id'] = int(match.group(1))
                current['slot'] = int(match.group(2))
                continue

        stripped = line.strip()
        for label, name in FIELDS.items():
            if stripped.startswith(f'{label}:'):
                current[name] = stripped.split(':', 1)[1].strip()
                break

    return conf


def render_drive(*, drive_id: int, library_id: int, slot: int, target: int,
                 vendor: str, product: str, serial: str,
                 channel: int = 0, lun: int = 0) -> str:
    """One Drive record, in MHVTL's own layout."""
    naa = f'{library_id:02d}:22:33:44:ab:{channel:02x}:{target:02x}:{lun:02x}'
    return (
        f'Drive: {drive_id:02d} CHANNEL: {channel:02d} TARGET: {target:02d} '
        f'LUN: {lun:02d}\n'
        f' Library ID: {library_id:02d} Slot: {slot:02d}\n'
        f' Vendor identification: {vendor}\n'
        f' Product identification: {product}\n'
        f' Unit serial number: {serial}\n'
        f' NAA: {naa}\n'
        f' Compression: factor 1 enabled 1\n'
        f' Compression type: lzo\n'
        f' Backoff: 400\n'
        f' # fifo: /var/tmp/mhvtl\n\n')


def remove_record(text: str, kind: str, device_id: int) -> Optional[str]:
    """Drop one Library or Drive record from the file.

    Returns None when the record cannot be found, so a caller can refuse to
    write rather than risk removing the wrong block.
    """
    lines = text.splitlines(keepends=True)
    header = re.compile(rf'^{kind}:\s*0*{device_id}\s+CHANNEL:')

    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        return None

    end = start + 1
    while end < len(lines) and not re.match(r'^(Drive|Library):', lines[end]):
        end += 1

    return ''.join(lines[:start] + lines[end:])


def record_text(text: str, kind: str, device_id: int) -> Optional[str]:
    """One Library or Drive record as it appears in the file, or None."""
    lines = text.splitlines(keepends=True)
    header = re.compile(rf'^{kind}:\s*0*{device_id}\s+CHANNEL:')

    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        return None

    end = start + 1
    while end < len(lines) and not re.match(r'^(Drive|Library):', lines[end]):
        end += 1
    return ''.join(lines[start:end])


def replace_field(text: str, kind: str, device_id: int, label: str, value: str) -> str:
    """Replace one ` Label: value` line inside a record."""
    lines = text.splitlines(keepends=True)
    header = re.compile(rf'^{kind}:\s*0*{device_id}\s+CHANNEL:')
    inside = False

    for index, line in enumerate(lines):
        if header.match(line):
            inside = True
            continue
        if inside:
            if re.match(r'^(Drive|Library):', line):
                break
            if line.strip().startswith(f'{label}:'):
                lines[index] = f' {label}: {value}\n'
                break
    return ''.join(lines)


def library_ids(text: str) -> List[int]:
    """Library ids in file order - useful without a full parse."""
    return [int(m.group(1)) for m in
            re.finditer(r'^Library:\s+(\d+)\b', text, re.MULTILINE)]


# ---------------------------------------------------------------------------
# Rendering a library and its drives
#
# Moved from mhvtl_library_service.py:_render_rpm_library_and_drives. It lives
# here because it writes device.conf records, and this module is what knows that
# format - the same reason render_drive() is here.
#
# The revision level matters more than it looks: backup software identifies a
# device by its SCSI inquiry fields, so a library written without "Product
# revision level" is reported differently to Veeam or NetBackup than the real
# hardware it emulates.
# ---------------------------------------------------------------------------

#: MHVTL truncates a unit serial number to this many characters.
MAX_SERIAL_LENGTH = 10


def _truncate_serial(serial: str) -> str:
    return (serial or '').strip()[:MAX_SERIAL_LENGTH]


def _default_home():
    """Where media lives, from settings rather than a hardcoded path."""
    from ..core import home_dir
    return home_dir()


#: The header MHVTL's own generate_device_conf writes at the top of a new
#: device.conf. Kept verbatim: ``VERSION: 5`` is what the daemons parse for, and
#: the comments are where an operator reads the field formats. A host creating
#: its first library has no device.conf, and one without this header is a file
#: unlike every other MHVTL installation's.
DEVICE_CONF_HEADER = r"""
VERSION: 5

# VPD page format:
# <page #> <Length> <x> <x+1>... <x+n>
# NAA format is an 8 hex byte value seperated by ':'
# Note: NAA is part of inquiry VPD 0x83
#
# Each 'record' is separated by one (or more) blank lines.
# Each 'record' starts at column 1
# Serial num max len is 10.
# Compression: factor X enabled 0|1
#     Where X is zlib compression factor	1 = Fastest compression
#						9 = Best compression
#     enabled 0 == off, 1 == on
#
# fifo: /var/tmp/mhvtl
# If enabled, data must be read from fifo, otherwise daemon will block
# trying to write.
# e.g. cat /var/tmp/mhvtl (in another terminal)

"""


def header_if_empty(text: str) -> str:
    """The header for an empty or absent device.conf, nothing for one that has
    content already. Prepending it twice would declare VERSION twice."""
    return '' if (text or '').strip() else DEVICE_CONF_HEADER.lstrip('\n')


def render_library_and_drives(existing_text: str, library_data: Dict[str, Any],
                              drive_targets: List[int], home_dir=None,
                              drive_ids: List[int] = None) -> str:
    """
    Render device.conf blocks for library and drives.

    Includes all identity-critical SCSI inquiry fields:
    - Vendor identification
    - Product identification
    - Product revision level (REQUIRED for backup software compatibility)
    - Unit serial number
    - NAA

    These fields are reported via SCSI inquiry and used by backup software
    (Veeam, NetBackup, Commvault, etc.) to identify the device.
    """
    # Imported here rather than at module scope: the profile tables are needed
    # only for rendering, and parsing should stay importable on its own.
    from ..profiles.data import get_profile

    library_id = int(library_data["library_id"])
    channel = int(library_data.get("channel", 0))
    lun = int(library_data.get("lun", 0))
    lib_target = int(library_data["target"])

    vendor = str(library_data["vendor"])
    product = str(library_data["product"])
    serial = _truncate_serial(str(library_data["serial"]))

    # Get library revision from profile (identity-critical for backup software)
    profile_key = library_data.get("profile") or library_data.get("vendor_profile") or library_data.get("vendor_key")
    try:
        profile = get_profile(profile_key)
        lib_revision = library_data.get("library_revision") or profile.library_revision_default
    except KeyError:
        lib_revision = "0001"  # Fallback if profile not found

    # Library block with all SCSI inquiry fields
    parts: List[str] = []
    parts.append(f"Library: {library_id:02d} CHANNEL: {channel:02d} TARGET: {lib_target:02d} LUN: {lun:02d}\n")
    parts.append(f" Vendor identification: {vendor}\n")
    parts.append(f" Product identification: {product}\n")
    parts.append(f" Product revision level: {lib_revision}\n")  # Issue #1: Now included
    parts.append(f" Unit serial number: {serial}\n")
    parts.append(f" NAA: {library_id:02d}:22:33:44:ab:{channel:02d}:{lib_target:02d}:{lun:02d}\n")
    parts.append(f" Home directory: {home_dir or _default_home()}\n")
    parts.append(" PERSIST: False\n")
    parts.append(" Backoff: 400\n")
    parts.append("# fifo: /var/tmp/mhvtl\n\n")

    # Drives: ids as allocated by config/ids, or library_id + slot when the
    # caller passes none (the RPM demo layout: 10 -> 11..14).
    num_drives = int(library_data.get("num_drives", 4))
    drv_vendor = str(library_data.get("drive_vendor"))
    drv_product = str(library_data.get("drive_product"))
    drv_rev = str(library_data.get("drive_revision", ""))

    # Drive ids come from config/ids when the caller has allocated them. The
    # fallback, library_id + slot, is only safe on an empty namespace: it is
    # what handed a tenth drive on library 10 the id 20.
    if drive_ids is not None and len(drive_ids) != num_drives:
        raise ValueError(f'{num_drives} drives but {len(drive_ids)} drive ids')

    for slot in range(1, num_drives + 1):
        did = drive_ids[slot - 1] if drive_ids is not None else library_id + slot
        dtgt = drive_targets[slot - 1]
        dserial = _truncate_serial(str(library_data.get("drive_serial", f"XYZZY_{did}")))

        # Drive block with all SCSI inquiry fields
        parts.append(f"Drive: {did:02d} CHANNEL: {channel:02d} TARGET: {dtgt:02d} LUN: {lun:02d}\n")
        parts.append(f" Library ID: {library_id:02d} Slot: {slot:02d}\n")
        parts.append(f" Vendor identification: {drv_vendor}\n")
        parts.append(f" Product identification: {drv_product}\n")
        parts.append(f" Product revision level: {drv_rev}\n")  # Issue #2: Now uncommented
        parts.append(f" Unit serial number: {dserial}\n")
        parts.append(f" NAA: {library_id:02d}:22:33:44:ab:{channel:02d}:{dtgt:02d}:{lun:02d}\n")
        parts.append(" Compression: factor 1 enabled 1\n")
        parts.append(" Compression type: lzo\n")
        parts.append(" Backoff: 400\n")
        parts.append("# fifo: /var/tmp/mhvtl\n\n")

    return "".join(parts)

