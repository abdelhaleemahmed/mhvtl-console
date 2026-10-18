"""What the library says about its drives, asked of the library itself.

READ ELEMENT STATUS with the volume tag and device identifier bits set. One
command per library, and it answers two questions at once for every drive:
**which cartridge is in it** and **which drive it is**.

WHY NOT mtx

`mtx status` gives the barcodes, and it is already parsed in mtx.py. What it
does not give is any way to say which drive it is talking about: it numbers
data transfer elements 0, 1, 2 from the library's point of view, and the
console addresses drives by their device.conf id. Turning one into the other
by position is exactly what vtlcmd.py refuses to do, and for a good reason -
a daemon holding stale state was seen reporting a library 10 tape in a
library 20 drive, and guessing that mapping is how an operation reaches the
wrong drive.

The device identifier settles it without guessing. Each element carries the
drive's own serial number, device.conf says which drive has which serial, and
matching on that is exact. Position is never used.

IT ANSWERS DURING A BACKUP

Like the log page in logsense.py, and for the same reason: this goes to the
library's generic node, and what a backup holds is the drive's /dev/nst node.
`mt` cannot open that; nothing here needs to.

WHAT A DESCRIPTOR LOOKS LIKE

Measured on an STK L80, not taken from the specification, because the offsets
move with the flags requested and the identifier area is a per-personality
width - 64 bytes on a Scalar, 34 on an IBM, 32 here:

    page header   byte 1 bit 7  PVolTag, set when volume tags were asked for
                  bytes 2-3     descriptor length, the stride

    descriptor    0 - 1         element address
                  2             flags, bit 0 Full
                  12 - 47       primary volume tag, 36 bytes, blank padded
                  48            code set
                  49            identifier type
                  50            reserved
                  51            identifier length
                  52 ...        identifier, the drive's serial

The volume tag is only present when PVolTag says so, which is why the page
header is read rather than assumed.

Rules for this layer:
    - all subprocess work goes through core.shell
    - never imports django.contrib.messages and never sees a request
    - addresses a library by its device.conf id, and resolves its node per
      call, because a daemon restart reshuffles every /dev/sg
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core import NORMAL, shell
from ..scsi import mapping

logger = logging.getLogger(__name__)

#: READ ELEMENT STATUS, element type 4 (data transfer), with VOLTAG in the top
#: nibble of byte 1 and DVCID in byte 6.
OPCODE = 'B8'
DATA_TRANSFER_WITH_VOLTAG = '14'
DVCID = '01'

#: Elements are scanned from address 0 rather than sought at the personality's
#: start_drive. The reply says where they really are, and a layout table has
#: been wrong about that before: the console's Overland entry says 255 and the
#: library answered from 1, because the configuration matched a different
#: personality than the table assumed.
FIRST_ELEMENT = ('00', '00')
HOW_MANY = ('00', '40')          # 64 drives, more than any library we offer
ALLOCATION = ('00', 'A0', '00')  # the CDB's three allocation-length bytes
ALLOCATION_BYTES = 0x00A000      # 40960: 64 descriptors of 88 and room over

#: Cleared by being reported, and a library has one pending after a restart.
UNIT_ATTENTION = 'unit attention'
ATTEMPTS = 3

HEADER = 8          # element status data header
PAGE_HEADER = 8     # element status page header
VOLUME_TAG = 36


@dataclass
class DriveElement:
    """One data transfer element, as the library describes it."""
    address: int
    full: bool
    barcode: Optional[str] = None
    identifier: Optional[str] = None

    def to_dict(self) -> Dict:
        return {'address': self.address, 'full': self.full,
                'barcode': self.barcode, 'identifier': self.identifier}

    def is_drive(self, serial: str) -> bool:
        """Whether this element is the drive with that device.conf serial.

        `identifier` is not the serial, and three different things come back
        depending on the personality and the drive, all measured on this host:

            XYZZY_B1                            STK, serial only
            SONY    SDX-900V        XYZZY_21    default, vendor+product+serial
            00XYZZY_A1                          IBM, zero padded to ten

        The last is upstream e43e2e3, "Zero-pad short IBM LTO drive serial
        numbers, as the drives do". So the serial is matched at the END of the
        identifier rather than against the whole of it, which holds for all
        three, and the caller checks that exactly one element matches.
        """
        return bool(self.identifier and serial
                    and self.identifier.endswith(serial))


def _text(raw: bytes) -> Optional[str]:
    """A blank or NUL padded SCSI field as a string, or None if it is empty.

    Spectra's T-series identifier is NUL terminated where everything else is
    blank filled, so both are stripped.
    """
    value = raw.decode('ascii', 'replace').strip('\x00 ').strip()
    return value or None


def parse(data: bytes) -> List[DriveElement]:
    """The drive elements in a READ ELEMENT STATUS reply.

    Returns an empty list for a reply with no elements, which is what a
    library answers when asked about a type it has none of.
    """
    if len(data) < HEADER + PAGE_HEADER:
        return []

    page = data[HEADER:HEADER + PAGE_HEADER]
    has_volume_tag = bool(page[1] & 0x80)
    stride = int.from_bytes(page[2:4], 'big')
    if stride <= 0:
        return []

    found = []
    at = HEADER + PAGE_HEADER
    while at + stride <= len(data):
        block = data[at:at + stride]
        element = DriveElement(address=int.from_bytes(block[0:2], 'big'),
                               full=bool(block[2] & 0x01))

        cursor = 12
        if has_volume_tag:
            element.barcode = _text(block[cursor:cursor + VOLUME_TAG])
            cursor += VOLUME_TAG

        if cursor + 4 <= len(block):
            length = block[cursor + 3]
            element.identifier = _text(block[cursor + 4:cursor + 4 + length])

        found.append(element)
        at += stride
    return found


def drives(library_id: int, *, config_dir=None,
           timeout: int = NORMAL) -> Optional[List[DriveElement]]:
    """Every drive element of one library, or None if it could not be asked.

    None means "no answer", never "no drives": a caller that cannot tell those
    apart would report a loaded library as empty.
    """
    # device_for_library already honours MHVTL_CHANGER_NODE and falls back to
    # the generic node, which is what mtx uses too - see scsi/mapping.py.
    node = mapping.device_for_library(int(library_id), config_dir=config_dir)
    if not node:
        logger.info('library %s has no generic node to read elements from',
                    library_id)
        return None

    # -b: the reply on stdout as bytes. Without it sg_raw prints a hex dump
    # for a human, which would have to be parsed back into the bytes it came
    # from.
    argv = ['sg_raw', '-b', '-r', str(ALLOCATION_BYTES), node,
            OPCODE, DATA_TRANSFER_WITH_VOLTAG, *FIRST_ELEMENT, *HOW_MANY,
            DVCID, *ALLOCATION, '00', '00']
    ok, raw, stderr = False, b'', ''
    for _ in range(ATTEMPTS):
        ok, raw, stderr = shell.sudo_bytes(argv, timeout=timeout)
        if ok or UNIT_ATTENTION not in stderr.lower():
            break

    if not ok:
        logger.info('READ ELEMENT STATUS on %s (library %s): %s',
                    node, library_id, stderr.strip()[:120])
        return None
    return parse(raw)


def loaded_by_drive(library_id: int, serials: Dict[int, str], *,
                    config_dir=None) -> Dict[int, Optional[str]]:
    """drive id -> the barcode the library says is in it, for the ones it knows.

    `serials` is drive id -> the serial from device.conf. A drive is left out
    of the answer entirely when the library did not describe it, or when its
    serial matched more than one element - the caller then knows it does not
    know, rather than being told the drive is empty.

    Empty when the library could not be asked at all. A caller must read that
    as "unknown", never as "nothing is loaded".
    """
    found = drives(library_id, config_dir=config_dir)
    if not found:
        return {}

    answer = {}
    for drive_id, serial in serials.items():
        matches = [element for element in found if element.is_drive(serial)]
        if len(matches) != 1:
            if matches:
                logger.info('library %s: serial %r matches %s drive elements; '
                            'not guessing which is drive %s',
                            library_id, serial, len(matches), drive_id)
            continue
        answer[drive_id] = matches[0].barcode if matches[0].full else None
    return answer
