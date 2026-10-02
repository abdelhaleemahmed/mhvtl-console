"""What a cartridge's own memory says about LTFS.

media.py answers one question - how full is this cartridge - from the `mam`
file and the sizes of the data files. This module answers a second one from a
*different* file with a *different* id namespace, which is why it is not more of
media.py: mhvtl writes its own attributes to `mhvtl_data`, not to `mam`
(write_mam() takes two file descriptors, usr/vtlcart.c:755).

Two things are easy to get wrong here and both have been measured rather than
assumed:

1. THE ID SPACES OVERLAP. `0x0001` is MAXIMUM CAPACITY in `mam` and FLAGS in
   `mhvtl_data`. They do not collide only because they are different files, so
   the file being read is part of the format. `mam` also has an 8-byte header
   and `mhvtl_data` has none.

2. THE VALUES ARE BIG-ENDIAN, the ids and lengths are not. mhvtl hands write()
   a pointer to a C struct field, which looks native, but every field was put
   there with put_unaligned_be16/32/64 first - usr/vtllib.c:1830 for tracks,
   usr/mode.c:784 for partition_capacity. Checked against a real cartridge:
   bits_per_mm reads 19107 big-endian and usr/vtllib.c:1901 writes
   put_unaligned_be32(19107, ...).

Rules for this module:
    - a READER, like media.py: it returns LtfsState, not a ServiceResult. The
      layer's "returns a ServiceResult" rule belongs to the services that act -
      tapes/service.py and ltfs/service.py wrap these values for a caller
    - its only I/O is through media.read_media_files(), so the layer's
      "subprocess work goes through core.shell" is honoured there and not here
    - never imports django.contrib.messages and never sees a request
    - only sync/ may import apps.libraries.models
"""
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import compatibility, media

#: mhvtl's own attributes live here, beside `mam`. Absent on 1.7 media.
VTL_FILE = 'mhvtl_data'

#: Unlike `mam`, this file has no version header: the first record is at 0.
VTL_HEADER = 0

#: ids from the INIT_VTL_ATTR() table, usr/vtllib.c:242-266.
VTL_MAX_PARTITIONS = 0x03
VTL_NUM_PARTITIONS = 0x04
VTL_MEDIA_TYPE = 0x05
VTL_DENSITY_NAME = 0x08
VTL_DESCRIPTION = 0x09
#: Coherency for partitions 1..3. Partition 0's lives in `mam` at 0x080c,
#: because only one of them fits in the SCSI attribute space
#: (include/vtllib.h:265).
VTL_COHERENCY_P1 = 0x0a
VTL_COHERENCY_P2 = 0x0b
VTL_COHERENCY_P3 = 0x0c
#: Written at format time only, so zero on a cartridge nothing has formatted.
VTL_PARTITION_CAPACITY = (0x0d, 0x0e, 0x0f, 0x10)

#: SCSI attributes in `mam` that say what wrote to the cartridge.
MAM_APPLICATION_VENDOR = 0x0800
MAM_APPLICATION_NAME = 0x0801
MAM_APPLICATION_VERSION = 0x0802
MAM_APPLICATION_FORMAT_VERSION = 0x080b
MAM_VOLUME_COHERENCY = 0x080c

#: The states a cartridge can be in, as reported. Four, not three, and the
#: difference between the last two is the one that matters:
#:
#:     LTFS       read, and it is an LTFS volume
#:     PLAIN      read, and it is not
#:     UNKNOWN    read, and the files could not say (1.7 media, or a torn read)
#:     NOT_ASKED  NOT READ AT ALL - a gate answered it first
#:
#: A skipped cartridge must never be reported PLAIN. It may well be an LTFS
#: volume written on another system: a library whose drives cannot mount it does
#: not make it any less of one, and an operator cannot see through a lie of that
#: shape.
LTFS = 'ltfs'
PLAIN = 'plain'
UNKNOWN = 'unknown'
NOT_ASKED = ''

#: LTO-5 was the first generation whose media can be partitioned, and LTFS needs
#: two partitions - so anything older can never hold an LTFS volume.
FIRST_PARTITIONABLE_LTO = 5


def read_vtl_attributes(blob: bytes) -> Dict[int, bytes]:
    """The attributes in an `mhvtl_data` file, by id.

    Stops at the first record that does not fit rather than raising - the same
    contract as media.read_mam(), for the same reason: mhvtl writes this file
    back when a cartridge is unloaded, so a read during a backup can catch a
    short or torn file. That is not a reason to fail a page.
    """
    found: Dict[int, bytes] = {}
    offset = VTL_HEADER
    while offset + 4 <= len(blob):
        attribute, length = struct.unpack_from('<HH', blob, offset)
        offset += 4
        if offset + length > len(blob):
            break
        found.setdefault(attribute, blob[offset:offset + length])
        offset += length
    return found


def _be(value: Optional[bytes]) -> Optional[int]:
    """A big-endian unsigned integer, or None when the field cannot say."""
    if not value or not any(value):
        return None
    return int.from_bytes(value, 'big')


def _text(value: Optional[bytes]) -> str:
    """A fixed-width text field, without its padding."""
    if not value:
        return ''
    return value.rstrip(b'\0 ').decode('latin1', 'replace')


def _is_set(value: Optional[bytes]) -> bool:
    return bool(value) and any(value)


@dataclass
class LtfsState:
    """Whether LTFS formatted this cartridge, and what it left behind."""
    barcode: str
    #: LTFS | PLAIN | UNKNOWN | NOT_ASKED. UNKNOWN is the default because a
    #: bare LtfsState has been read and could not say; NOT_ASKED is set
    #: deliberately by a gate and means the cartridge was never touched.
    state: str = UNKNOWN
    #: Partitions counted from the cartridge's data.N files, never from the MAM.
    partitions: int = 0
    #: Size of each partition as the format wrote it. None where unformatted -
    #: never 0, because 0 reads as "empty" and sends an operator looking.
    partition_capacities: List[Optional[int]] = field(default_factory=list)
    #: What LTFS stamped into the MAM. Empty when nothing did.
    application_name: str = ''
    application_version: str = ''
    format_version: str = ''
    #: True when a coherency record is present on any partition.
    coherency_present: bool = False
    #: True when the MAM carries LTFS traces but the cartridge is no longer
    #: partitioned - `mkltfs --wipe` leaves every attribute behind. Such a
    #: cartridge cannot be mounted, so it is reported `plain`, and this says why
    #: its MAM disagrees.
    was_ltfs: bool = False
    #: What the MAM claims, kept only to show that it disagrees.
    stored_partition_count: Optional[int] = None
    #: How many partitions this cartridge could have, as the format recorded it.
    #: The ceiling, against `partitions` as the actual count.
    max_partitions: Optional[int] = None
    #: mhvtl's own names for the media, from mhvtl_data - 'U-732',
    #: 'Ultrium 7/32T'. Useful when a barcode suffix and a cartridge disagree.
    density_name: str = ''
    media_description: str = ''
    #: The MEDIA TYPE byte, as mhvtl recorded it.
    media_type: Optional[int] = None

    @property
    def partitioned(self) -> bool:
        return self.partitions >= 2

    @property
    def summary(self) -> str:
        """One phrase, worded here so the page, the inventory and the CLI all
        say the same thing about the same cartridge.

        Empty for NOT_ASKED. A cartridge nothing looked at has no summary to
        give, and "not known" would sound like an answer.
        """
        if self.state == NOT_ASKED:
            return ''
        if self.state == UNKNOWN:
            return 'not known'
        if self.state == LTFS:
            version = f' {self.format_version}' if self.format_version else ''
            return f'LTFS{version}, {self.partitions} partitions'
        if self.was_ltfs:
            return 'not LTFS - formatted once, since unpartitioned'
        return 'not LTFS'

    def to_dict(self) -> Dict:
        return {
            'barcode': self.barcode,
            'state': self.state,
            'partitions': self.partitions,
            'partition_capacities': self.partition_capacities,
            'application_name': self.application_name,
            'application_version': self.application_version,
            'format_version': self.format_version,
            'coherency_present': self.coherency_present,
            'was_ltfs': self.was_ltfs,
            'stored_partition_count': self.stored_partition_count,
            'max_partitions': self.max_partitions,
            'density_name': self.density_name,
            'media_type': self.media_type,
            'media_description': self.media_description,
            'summary': self.summary,
        }


def could_hold_ltfs(barcode: str) -> bool:
    """Could this cartridge ever be an LTFS volume, judged from its barcode?

    False only for a generation known to be too old. LTO-5 introduced media
    partitioning and LTFS needs two partitions, so LTO-1 to LTO-4 can be answered
    without being read.

    A barcode with NO LTO generation returns True, deliberately. It is not an
    LTO cartridge, which does not mean it cannot hold LTFS: IBM 3592 media is
    partitionable and LTFS supports TS1140 and later. Treating "unrecognised" as
    "impossible" would silently report a 3592 LTFS volume as a plain cartridge.
    Only what is known to be too old is filtered.
    """
    generation = compatibility.lto_for_barcode(barcode)
    if not generation:
        return True
    try:
        return int(generation.split('-')[1]) >= FIRST_PARTITIONABLE_LTO
    except (IndexError, ValueError):
        return True


def state_from_files(barcode: str, vtl_blob: Optional[bytes],
                     mam_blob: Optional[bytes], partitions: int) -> LtfsState:
    """Decide from the two files and the partition count.

    partitions comes from the caller because it is counted from the data.N files
    on disk, which media.usage_for_all() already enumerates - and which is the
    only authoritative source. mhvtl recomputes it the same way at every load
    (usr/vtlcart.c:1351); the MAM's NUM_PARTITIONS is stale on any cartridge
    mktape made and nothing has formatted.
    """
    result = LtfsState(barcode=barcode, partitions=partitions)
    if not vtl_blob:
        # 1.7 media, or a cartridge we could not read. Either way: cannot say.
        return result

    vtl = read_vtl_attributes(vtl_blob)
    scsi = media.read_mam(mam_blob) if mam_blob else {}

    result.stored_partition_count = _be(vtl.get(VTL_NUM_PARTITIONS))
    result.max_partitions = _be(vtl.get(VTL_MAX_PARTITIONS))
    result.density_name = _text(vtl.get(VTL_DENSITY_NAME))
    result.media_type = _be(vtl.get(VTL_MEDIA_TYPE))
    result.media_description = _text(vtl.get(VTL_DESCRIPTION))
    result.partition_capacities = [_be(vtl.get(i))
                                   for i in VTL_PARTITION_CAPACITY[:max(partitions, 1)]]

    result.application_name = _text(scsi.get(MAM_APPLICATION_NAME))
    result.application_version = _text(scsi.get(MAM_APPLICATION_VERSION))
    result.format_version = _text(scsi.get(MAM_APPLICATION_FORMAT_VERSION))
    result.coherency_present = any(
        _is_set(blob) for blob in
        (scsi.get(MAM_VOLUME_COHERENCY), vtl.get(VTL_COHERENCY_P1),
         vtl.get(VTL_COHERENCY_P2), vtl.get(VTL_COHERENCY_P3)))

    # APPLICATION_VENDOR is NOT a signal: mktape sets it to 'vtl-1.8' on every
    # cartridge it makes, and LTFS overwrites it with 'IBM' - at no point does it
    # say LTFS. APPLICATION_NAME is the field that does.
    traces = result.coherency_present or result.application_name.startswith('LTFS')

    if traces and result.partitioned:
        result.state = LTFS
    else:
        result.state = PLAIN
        result.was_ltfs = traces
    return result


def state_for_all(barcode_list: List[str], partitions: Dict[str, int],
                  base=None) -> Dict[str, LtfsState]:
    """LTFS state for many cartridges, in one pass.

    Plural first, singular delegating to it - the shape media.usage_for_all()
    established, and for the same measured reason: per-cartridge reads cost
    about 69ms each, so a 32-cartridge library spent over four seconds shelling
    out on every page load.

    partitions is {barcode: count} from media.usage_for_all(); a barcode missing
    from it is treated as having none counted, which reports `unknown` rather
    than guessing.
    """
    wanted = list(barcode_list)
    if not wanted:
        return {}

    # A cartridge too old to be partitioned cannot be an LTFS volume, and its
    # generation is in its barcode - so it is answered without being read.
    readable = [b for b in wanted if could_hold_ltfs(b)]
    skipped = [b for b in wanted if b not in set(readable)]

    files = (media.read_media_files(readable, ('mam', VTL_FILE), base)
             if readable else {})
    found = {
        barcode: state_from_files(
            barcode,
            files.get(barcode, {}).get(VTL_FILE),
            files.get(barcode, {}).get('mam'),
            partitions.get(barcode, 0))
        for barcode in readable
    }
    # Reported as nothing-asked rather than `plain`: the cartridge was not read,
    # and "we did not look" is not the same answer as "we looked and it is not
    # LTFS". See LtfsState.state and TapeInfo.ltfs_state.
    for barcode in skipped:
        found[barcode] = LtfsState(barcode=barcode, state=NOT_ASKED,
                                   partitions=partitions.get(barcode, 0))
    return found


def state(barcode: str, partitions: int, base=None) -> LtfsState:
    """One cartridge. Prefer state_for_all() when asking about many."""
    found = state_for_all([barcode], {barcode: partitions}, base)
    return found.get(barcode, LtfsState(barcode=barcode))
