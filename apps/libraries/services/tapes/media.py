"""The tape files on disk, under the media directory.

Gathers tape_operations_service.py:1934 (mktape), :2060 (the rm -rf),
:2682 (_get_tape_size_mb) and :2716 (_get_tape_used_mb).

Three things are different here, each for a reason found on a running host.

1. BOTH MEDIA LAYOUTS. mhvtl 1.7 wrote one set of files per tape:

       E01001L8/data  E01001L8/indx  E01001L8/meta

   1.8 splits them per LTFS partition, and adds a MAM file:

       E01001L8/data.0  E01001L8/indx.0  E01001L8/meta.0  E01001L8/mam

   Code that looked for a file named exactly `data` reported every tape as
   0 bytes after the upgrade, silently.

2. CAPACITY COMES FROM THE MAM, WHICH SAYS WHAT IT HOLDS. The old reader took
   a big-endian u64 at offset 8 of `meta` and called it the capacity. In 1.8
   those bytes are zeros, so it returned 0 - a tape that looks empty and full
   at once. This module then reported the capacity as unknown rather than
   guess, which left every unloaded tape without a size.

   The MAM file is not a guess, though: mhvtl writes it as type-length-value
   "to keep mam auto-descriptive" (usr/vtlcart.c:write_mam), after two uint32
   version fields, and the ids are the SCSI ones - 0x0000 remaining capacity,
   0x0001 maximum capacity, both 8-byte big-endian (usr/vtllib.c:188). Reading
   those two is reading what the file says it contains, and it answers for a
   tape sitting in a slot, which `vtlcmd stats` cannot.

   The drive is still the authority for a tape it holds: the MAM is written
   back when the tape is unloaded, so mid-backup it is behind - 59.2% on disk
   while the drive reported 75.7%.

3. DELETION IS CONTAINED. The barcode is validated and the resolved path is
   checked to be a direct child of the media directory before anything is
   removed. The original interpolated the barcode straight into
   `sudo rm -rf /opt/mhvtl/<barcode>`.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import io
import logging
import re
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..core import NORMAL, QUICK, CommandResult, home_dir, media_dir, shell
from . import barcodes

logger = logging.getLogger(__name__)

#: Data files, either layout: `data` (1.7) or `data.0`, `data.1` (1.8).
DATA_FILE_RE = re.compile(r'^data(\.\d+)?$')
MEGABYTE = 1024 * 1024


class MediaPathRefused(ValueError):
    """The barcode does not resolve to a path inside the media directory."""


def path_for(barcode: str, base=None) -> Path:
    """Where a tape's files live, refusing anything that escapes the directory.

    Two checks, because this feeds `rm -rf`: the barcode must be a barcode, and
    the resolved path must be a direct child of the media directory.
    """
    barcodes.validate(barcode)

    root = Path(base).resolve() if base else home_dir().resolve()
    candidate = (root / barcode).resolve()
    if candidate.parent != root:
        raise MediaPathRefused(
            f'{barcode!r} resolves to {candidate}, which is not inside {root}')
    return candidate


@dataclass
class MediaUsage:
    """What a tape's files say about it."""
    barcode: str
    exists: bool = False
    used_bytes: int = 0
    capacity_bytes: Optional[int] = None      # None means "not known from disk"
    remaining_bytes: Optional[int] = None     # what the MAM says is left
    layout: str = ''                          # '1.7' | '1.8' | ''

    @property
    def used_mb(self) -> int:
        return self.used_bytes // MEGABYTE

    @property
    def capacity_mb(self) -> Optional[int]:
        return self.capacity_bytes // MEGABYTE if self.capacity_bytes else None

    @property
    def used_percent(self) -> Optional[float]:
        if not self.capacity_bytes:
            return None
        return round(self.used_bytes / self.capacity_bytes * 100, 1)

    def to_dict(self) -> Dict:
        return {
            'barcode': self.barcode,
            'exists': self.exists,
            'used_bytes': self.used_bytes,
            'used_mb': self.used_mb,
            'capacity_mb': self.capacity_mb,
            'used_percent': self.used_percent,
            'layout': self.layout,
        }


#: The MAM is TLV after two uint32 version fields; these are the SCSI
#: attribute ids for what a tape holds, each an 8-byte big-endian count of
#: bytes (mhvtl usr/vtllib.c:188).
MAM_HEADER = 8
#: A MAM is about a kilobyte; anything far larger is not one.
MAM_LIMIT = 1024 * 1024
MAM_REMAINING_CAPACITY = 0x0000
MAM_MAX_CAPACITY = 0x0001


def read_mam(blob: bytes) -> Dict[int, bytes]:
    """The attributes in a MAM file, by id.

    Stops at the first record that does not fit rather than raising: a MAM
    being rewritten as this reads it is a short or torn file, not a reason to
    fail a page.
    """
    found: Dict[int, bytes] = {}
    offset = MAM_HEADER
    while offset + 4 <= len(blob):
        attribute, length = struct.unpack_from('<HH', blob, offset)
        offset += 4
        if offset + length > len(blob):
            break
        found.setdefault(attribute, blob[offset:offset + length])
        offset += length
    return found


def capacity_from_mam(blob: bytes):
    """(capacity, remaining) in bytes, or (None, None) if the file cannot say."""
    attributes = read_mam(blob)
    capacity = attributes.get(MAM_MAX_CAPACITY)
    remaining = attributes.get(MAM_REMAINING_CAPACITY)
    if not capacity or len(capacity) != 8:
        return None, None
    total = struct.unpack('>Q', capacity)[0]
    if not total:
        return None, None
    left = (struct.unpack('>Q', remaining)[0]
            if remaining and len(remaining) == 8 else None)
    # A remaining larger than the tape is a MAM that has not been written yet.
    if left is not None and left > total:
        left = None
    return total, left


def read_mams(barcode_list: List[str], base=None) -> Dict[str, bytes]:
    """Every tape's MAM file in one call, for the same reason usage_for_all
    exists: one sudo per page, not one per tape.

    Through tar, because the sudoers rules allow tar and do not allow a shell
    - and should not: a rule for `sh -c` is a rule for anything. tar names
    each member, so the files come back separated and identified, which `cat`
    of several files does not manage.
    """
    root = Path(base).resolve() if base else home_dir().resolve()
    names = []
    for barcode in barcode_list:
        try:
            path_for(barcode, base)                 # refuses anything odd
        except (barcodes.InvalidBarcode, MediaPathRefused):
            continue
        names.append(f'{barcode}/mam')
    if not names:
        return {}

    # -C root with relative names: the archive then holds "BARCODE/mam", and
    # nothing in it can name a path outside the media directory.
    ok, archive, error = shell.sudo_bytes(
        ['tar', '-cf', '-', '-C', str(root), '--ignore-failed-read', *names],
        timeout=NORMAL)
    if not archive:
        if not ok:
            logger.warning('cannot read MAM files under %s: %s', root,
                           error.strip()[:200])
        return {}

    blobs = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for member in bundle.getmembers():
                if not member.isfile() or member.size > MAM_LIMIT:
                    continue
                handle = bundle.extractfile(member)
                if handle is None:
                    continue
                blobs[Path(member.name).parent.name] = handle.read()
    except tarfile.TarError:
        logger.warning('the MAM archive from %s could not be read', root,
                       exc_info=True)
    return blobs


def exists(barcode: str, base=None) -> bool:
    try:
        return path_for(barcode, base).is_dir()
    except (barcodes.InvalidBarcode, MediaPathRefused):
        return False


def usage(barcode: str, base=None) -> MediaUsage:
    """One tape's disk usage. Prefer usage_for_all() when asking about many."""
    return usage_for_all([barcode], base).get(barcode, MediaUsage(barcode=barcode))


def usage_for_all(barcode_list: List[str], base=None) -> Dict[str, MediaUsage]:
    """Disk usage for many tapes in one pass.

    One `find` over the media directory instead of two sudo calls per tape. The
    old per-tape reads cost about 69ms each, so listing a 32-tape library spent
    over four seconds shelling out - on every page load.
    """
    root = Path(base) if base else home_dir()
    wanted = {barcode: MediaUsage(barcode=barcode) for barcode in barcode_list}
    with_mam: List[str] = []
    if not wanted:
        return {}

    listing = shell.sudo(
        ['find', str(root), '-mindepth', '2', '-maxdepth', '2', '-type', 'f',
         '-printf', '%h/%f %s\n'], timeout=NORMAL)

    if not listing.ok:
        logger.warning('cannot measure media in %s: %s', root,
                       listing.output.strip()[:200])
        return wanted

    for line in listing.stdout.splitlines():
        path, _, size = line.rpartition(' ')
        if not path or not size.isdigit():
            continue

        file_path = Path(path)
        entry = wanted.get(file_path.parent.name)
        if entry is None:
            continue

        entry.exists = True
        name = file_path.name
        if DATA_FILE_RE.match(name):
            entry.used_bytes += int(size)
            entry.layout = '1.8' if '.' in name else '1.7'
        elif name == 'mam':
            entry.layout = entry.layout or '1.8'
            with_mam.append(entry.barcode)

    # What each tape holds, from the tape's own MAM - the only source that
    # answers for a tape sitting in a slot.
    for barcode, blob in read_mams(with_mam, base).items():
        entry = wanted.get(barcode)
        if entry is None:
            continue
        entry.capacity_bytes, entry.remaining_bytes = capacity_from_mam(blob)

    return wanted


def create(barcode: str, *, library_id: int, size_mb: int, density: str,
           kind: str = 'data', base=None) -> CommandResult:
    """Create a tape's files with mktape.

    mktape writes into the library's home directory itself; base is accepted so
    a test can point at a scratch directory.
    """
    barcodes.validate(barcode)
    argv = ['mktape', '-l', str(library_id), '-m', barcode,
            '-s', str(size_mb), '-t', kind, '-d', density]
    if base:
        argv += ['-H', str(base)]
    return shell.sudo(argv, timeout=NORMAL)


def delete(barcode: str, base=None) -> CommandResult:
    """Remove a tape's files.

    path_for() has already refused anything that is not a plain barcode
    resolving inside the media directory, so the path handed to rm is one this
    module built, not one a caller supplied.
    """
    target = path_for(barcode, base)
    if not target.is_dir():
        return CommandResult([str(target)], 0, '', 'nothing to remove')
    return shell.sudo(['rm', '-rf', str(target)], timeout=QUICK)


def list_media(base=None) -> List[str]:
    """Every barcode with a directory on disk, whether or not a library knows it."""
    root = Path(base) if base else home_dir()
    result = shell.sudo(['find', str(root), '-mindepth', '1', '-maxdepth', '1',
                         '-type', 'd', '-printf', '%f\n'], timeout=QUICK)
    if not result.ok:
        logger.warning('cannot list media in %s: %s', root,
                       result.output.strip()[:200])
        return []
    return sorted(name for name in result.stdout.split() if name)
