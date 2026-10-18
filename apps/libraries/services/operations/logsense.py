"""Drive counters read from LOG SENSE, which answers during a backup.

THIS REPLACES `vtlcmd stats`, AND IT IS THE ONLY ROUTE THAT WORKS UPSTREAM.

`vtlcmd <drive> stats` was our own addition to MHVTL, sent as pull request 210
and **closed without being merged** on 3 October 2026. A host running upstream
MHVTL answers:

    Please check command, parameter 'check param' wrong.

and every drive in `status activity` reads "silent". The replacement is our
pull request 211, merged on 12 October, which fills in the megabyte counters
of the volume statistics log page, 17h. This module reads that page.

WHY IT STILL ANSWERS WHILE A BACKUP HOLDS THE DRIVE

Not because it avoids SCSI - it is a SCSI command. `mt` fails on `open()` of
/dev/nst*, because the st driver gives one writer exclusive use of that node.
The generic node, /dev/sg*, is a different driver on the same target and is not
taken. Measured on this host during a 210 MiB write: `mt` returned "Device or
resource busy" throughout while the page below was read three times and moved
26 -> 54 -> 114 MB.

WHAT THE PAGE DOES AND DOES NOT KNOW

It carries the bytes. It does not carry the cartridge: `Volume serial number`
and `Volume barcode` are both empty on an MHVTL drive, and nothing on the page
says whether a tape is loaded. Both belong to the robot anyway, which is the
authority on what is where - so `barcode` and `loaded` are left for the caller
to fill from its own listing, and this module never guesses them.

A TRAP IN THE FIGURES

`Total native capacity [MB]` is **bytes**, despite the label: 524288000 is a
500 MB cartridge, not 500 million MB. That is the capacity_unit question raised
in pull request 211 and left for the maintainer. The percentage survives it
because both sides of the division carry the same wrong unit. Anything printed
as a byte count would not, so the two capacity figures are used together and
never alone.

Rules for this layer:
    - returns a CommandResult-derived value through core.shell, never raw
      subprocess
    - never imports django.contrib.messages and never sees a request
    - addresses a drive by its device.conf id, and resolves the node per call
      because a daemon restart reshuffles every /dev/sg* and /dev/st*
"""
import logging
import re
from typing import Dict, Optional

from ..core import NORMAL, shell
from ..scsi import mapping
from .vtlcmd import TapeStats

logger = logging.getLogger(__name__)

#: The volume statistics page. 0x0c, the sequential access page, carries the
#: tape position and is not needed: 17h gives capacity and used capacity in the
#: same unit, which is the percentage, and the compression ratio besides.
VOLUME_STATISTICS = '0x17'

#: Each line of `sg_logs` output is "  Some label: value". Only these are read;
#: the page has forty-odd parameters and the rest are not ours to interpret.
FIELDS = {
    'last mount megabytes written': 'mount_mb_written',
    'last mount megabytes read': 'mount_mb_read',
    'lifetime megabytes written': 'lifetime_mb_written',
    'lifetime megabytes read': 'lifetime_mb_read',
    'last load write compression ratio': 'write_ratio',
    'last load read compression ratio': 'read_ratio',
    'total native capacity [mb]': 'capacity',
    'total used native capacity [mb]': 'used',
}

LINE_RE = re.compile(r'^\s*([A-Za-z][^:]*?)\s*:\s*(-?\d+)\s*$')

#: With no cartridge loaded MHVTL reports both capacity figures as 0xFFFFFFFE,
#: a placeholder it uses rather than a real size - see the "avoid size issues
#: with LTFS" workaround in usr/mhvtl_log.c. Measured here on two empty drives,
#: one of which had held a tape and one of which never had. It is the only
#: thing on this page that says whether a tape is present, so it is read as
#: exactly that, and a percentage is never calculated from it: capacity and
#: used are equal, so the drive would report itself 100% full.
NO_CARTRIDGE = 0xFFFFFFFE

#: What a tape cartridge can plausibly hold, used to tell which unit the
#: capacity pair is counted in - see capacity_unit(). Deliberately wide: the
#: smallest MHVTL makes is a few megabytes and the largest real cartridge is
#: tens of terabytes, and the two readings of any value are 2^20 apart, so
#: nothing inside this window is ambiguous.
SMALLEST_CARTRIDGE = 100 * 1000 * 1000             # 100 MB
LARGEST_CARTRIDGE = 100 * 1000 * 1000 * 1000 * 1000  # 100 TB

#: A drive answers with a unit attention rather than data after a load, and
#: again after a daemon restart. It is not an error: each pending condition
#: clears as it is reported, so a device with more than one takes more than one
#: command to get past them.
#:
#: Measured: after `systemctl restart mhvtl.target`, a single retry left every
#: drive in a library reading "silent" on the first `status activity` and
#: correct on the second. Clearing them is part of reading the page, not
#: something a caller should have to know about.
UNIT_ATTENTION = 'unit attention'

#: Attempts before giving up, counting the first. Three covers the restart
#: case measured above with one spare; a device answering unit attention four
#: times running has something wrong with it that a retry will not fix.
ATTEMPTS = 3


def parse_page(text: str) -> Dict[str, int]:
    """The numbers this module uses, keyed by the names in FIELDS.

    Unrecognised lines are skipped rather than refused: the page carries
    partition tables and hex payloads that are none of our business, and a new
    firmware adding a parameter must not stop the rest being read.
    """
    found = {}
    for line in text.splitlines():
        match = LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1).strip().lower()
        if label in FIELDS:
            found[FIELDS[label]] = int(match.group(2))
    return found


def capacity_unit(page: Dict[str, int]) -> Optional[int]:
    """How many bytes one unit of the capacity pair is, derived not assumed.

    THE CAPACITY PAIR IS NOT IN BYTES, AND NOT ALWAYS IN THE SAME THING.

    `Total native capacity` and `Total used native capacity` are counted in
    `lu_priv->capacity_unit`, which a drive personality chooses. Both of these
    are real, from two drives on one host on the same afternoon:

        STK T10000B      capacity 524288000   used 221089482    unit 1
        IBM ULT3580-TD8  capacity        500  used        401   unit 2^20

    The label says "[MB]" in both cases and means something different each
    time. A percentage is safe either way, because both sides of the division
    carry the same unit - but a byte count is not, and reading 401 units as
    401 bytes is how a tape four fifths full reported "401 B written".

    THE DISCRIMINATOR IS THE CAPACITY, NOT THE COUNTERS

    The first version of this divided a megabyte counter by the used figure,
    on the reasoning that both describe the same bytes and the megabyte
    counters are pinned by the specification at 10^6. They do describe the
    same bytes - until the tape is rewound and written again, when the
    counter keeps climbing and the used figure starts over:

        LTO, one pass        561 MB written, 401 units     ratio 1.05e6
        LTO, after rewinds   561 MB written, 132 units     ratio 4.25e6

    It survived that by luck. It did not survive the same thing on a
    byte-unit drive, where four rewrites turned 221 MB of tape into a
    reported 231 TERABYTES. Shipped in 3.5.1 and caught by somebody asking a
    question about compression ratios.

    So the unit is decided by what makes the CAPACITY a believable cartridge,
    which does not move when anything is written:

        LTO      capacity 500         x 1 = 500 B        no
                                      x 2^20 = 524 MB    yes -> 2^20
        T10000B  capacity 524288000   x 1 = 524 MB       yes -> 1
                                      x 2^20 = 550 TB    no

    The window is 100 MB to 100 PB, and it cannot be ambiguous: a value whose
    byte reading is at least 100 MB has a 2^20 reading of at least 100 TB, so
    only one of the two can land inside it. A 12 TB LTO-8 resolves either way
    round - 12e12 as bytes, or 11,444,092 as 2^20 units.

    Returns None for a capacity that is neither, which is a page this code
    does not understand rather than a cartridge it can measure.
    """
    capacity = page.get('capacity', 0)
    if capacity <= 0 or capacity == NO_CARTRIDGE:
        return None

    for unit in (1, 1 << 20):
        if SMALLEST_CARTRIDGE <= capacity * unit <= LARGEST_CARTRIDGE:
            return unit
    return None


def to_stats(page: Dict[str, int]) -> Optional[TapeStats]:
    """A TapeStats from the parsed page, or None if the page said nothing.

    `barcode` is always None. The page's own barcode fields are empty on MHVTL,
    and the caller has the robot's listing, which is the authority.
    """
    if not page:
        return None

    capacity = page.get('capacity', 0)
    used = page.get('used', 0)
    if capacity == NO_CARTRIDGE:
        return TapeStats(barcode=None, loaded=False)

    unit = capacity_unit(page)
    if unit is None:
        # A loaded cartridge nothing has been written to. The percentage is
        # zero whatever the unit is, and there are no bytes to report.
        return TapeStats(barcode=None, loaded=True, capacity=capacity)

    written_media = used * unit
    read_media = 0

    # The compression ratio, parameter 0012h, is the ratio x 100 - so the
    # initiator figure is the one it implies. Zero on a daemon without our
    # patch for 0012h and 0013h, and the two are then reported equal, which
    # reads as 1:1 and is not claimed as compression anywhere.
    write_ratio = page.get('write_ratio', 0)
    written = int(written_media * write_ratio / 100) if write_ratio else written_media

    return TapeStats(barcode=None, loaded=True,
                     written=written, read=0,
                     written_media=written_media, read_media=read_media,
                     capacity=capacity * unit)


def read(drive_id: int, *, config_dir=None, timeout: int = NORMAL) -> Optional[TapeStats]:
    """Live counters for one drive, or None if the page could not be read.

    None means "no answer", never "nothing written": a caller that cannot tell
    those apart would report a busy drive as idle.
    """
    node = mapping.device_for_drive(int(drive_id), config_dir=config_dir,
                                    generic=True)
    if not node:
        logger.info('drive %s has no generic node to read the log page from',
                    drive_id)
        return None

    argv = ['sg_logs', f'--page={VOLUME_STATISTICS}', node]
    for _ in range(ATTEMPTS):
        result = shell.sudo(argv, timeout=timeout)
        # Both streams, deliberately. sg_logs prints the vendor and model to
        # stdout even when the command failed, so CommandResult.output - which
        # is stdout whenever stdout said anything - never shows the sense data
        # that went to stderr, and a check against it silently never matches.
        said = f'{result.stdout}\n{result.stderr}'.lower()
        if result.ok or UNIT_ATTENTION not in said:
            break

    if not result.ok:
        logger.info('sg_logs 17h %s (drive %s): %s', node, drive_id,
                    result.output.strip()[:120])
        return None

    page = parse_page(result.stdout)
    if not page:
        # Asked, answered, and not understood - which is a different fault
        # from not being able to ask, and used to look identical to it. The
        # drive reported "silent" on camera twice while the daemon's own log
        # showed it serving the LOG SENSE, and there was nothing to say which
        # half had failed. Whatever it said is logged, so next time there is.
        logger.warning(
            'sg_logs 17h %s (drive %s) succeeded but no known field was in '
            'its answer; the first 400 characters were: %r',
            node, drive_id, result.stdout.strip()[:400])
        return None
    return to_stats(page)
