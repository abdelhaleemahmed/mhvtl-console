"""Which drives LTFS will open, and which it refuses.

This is deliberately *not* in personalities.py. That module is a transcription
of what MHVTL emulates - our own stack, describing itself. Whether LTFS will
open a drive is a fact about **LTFS**, a separate upstream project, which
happens to be decided from the same two strings device.conf already carries.
Keeping them apart means adding a library model never requires knowing anything
about LTFS, and the tables below can be re-checked against LTFS on their own.

THE SEPARATION RUNS ONE WAY. LTFS DOES NOT GET A VOTE ON THE PROFILES.
----------------------------------------------------------------------

This module reads what a profile says and answers a question about it. It must
never become a reason to change what a profile says. profiles/data.py and
profiles/personalities.py describe real hardware and what MHVTL emulates for
it - both verified complete against the MHVTL source, both held there by 127
tests - and "LTFS would open more drives if this table said something else" is
not an argument for editing either of them.

When a library's drives are ones LTFS refuses, the supported answer is
libraries/workflow.add_ltfs_drive_workflow(): it adds ONE drive whose vendor id
LTFS knows, pairing it with a model the library's own profile already takes, and
leaves every existing drive claiming exactly what it claimed before. That is a
per-library operator decision, which is where it belongs - not a global edit to
a description of hardware.

See personalities.py's header for the asymmetry that makes this safe at all: a
drive's vendor id is inert inside MHVTL (config_lu() matches on the product id
alone, usr/cmd/vtltape.c:1897), while a LIBRARY's vendor id selects the emulated
robot (customise_lu(), usr/cmd/vtllibrary.c:1461). Inert inside MHVTL is not the
same as free to change: it is what backup software identifies a device by, and
what LTFS matches on.

LTFS decides twice, differently, and the difference matters to a caller:

    enumeration   `ltfs -o device_list` names a drive through
                  _generate_product_name() (sg_tape.c:4358), which matches on
                  PRODUCT ID ALONE, and only against the IBM and HP tables. So
                  a drive can be listed and still be refused. The device list is
                  not a compatibility list.

    opening       _raw_open() (sg_tape.c:501) matches VENDOR AND PRODUCT against
                  the table for that vendor, then checks the drive's firmware
                  revision. This module answers for *opening*, which is the only
                  question an operator cares about.

Source: LinearTapeFileSystem/ltfs v2.4.9.0-10523 (efc9e3a). Checked against that
tree by tests/test_ltfs.py whenever it is present; see LTFS_SOURCE there.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models

This module does no I/O at all: it is tables and comparisons.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import personalities

#: The LTFS tree the references below point into.
LTFS_REVISION = 'v2.4.9.0-10523'

#: The only vendor ids get_vendor_id() maps to a table (vendor_compat.c:314).
#: Anything else is VENDOR_UNKNOWN, and get_supported_devs() returns NULL for it
#: - which until our patch was a SIGSEGV rather than a refusal. See
#: patches/ltfs-upstream/ and docs guides/ltfs-install.rst.
KNOWN_VENDORS = ('IBM', 'HP', 'HPE', 'QUANTUM')

#: TANDBERG is defined as a vendor id (hp_tape.h:70) and has an entry in
#: hp_supported_drives, but get_vendor_id() has no branch for it, so that entry
#: can never match. Recorded so nobody adds it to KNOWN_VENDORS on the strength
#: of the table.
UNREACHABLE_VENDORS = ('TANDBERG',)

#: NOT an asymmetry, though it reads like one: sg_is_mountable() and
#: sg_is_readonly() (sg_tape.c:4903, 4922) call ibm_tape_is_mountable() for
#: EVERY vendor, and no hp_tape_is_mountable() exists. That is correct.
#: _is_mountable() (ibm_tape.c:1187) branches on the drive family - IS_LTO()
#: picks lto_drive_density, everything else picks jaguar_drive_density - and the
#: LTO tables are the consortium's generation rules, identical for an IBM, HP/HPE
#: or Quantum LTO drive. Only the Jaguar branch is IBM-specific, and no other
#: vendor builds a TS11xx, so a non-IBM drive never reaches it.
#:
#: Written down because deriving it costs an hour. The firmware gate below IS
#: vendor-dispatched; mountability is not. Do not "fix" either to match the
#: other.

#: Minimum firmware revision per VENDOR, then per drive family.
#:
#: Keyed by vendor because the gate is vendor-dispatched upstream:
#:
#:     drive_has_supported_fw()                     vendor_compat.c:347
#:         case VENDOR_IBM: ibm_tape_is_supported_firmware(...)
#:         default:         return true
#:
#: There is no hp_tape_is_supported_firmware() or quantum equivalent to call -
#: hp_tape.c and quantum_tape.c are three tables and a timeout function each, and
#: only ibm_tape.c carries the check. So an HPE 'Ultrium 8-SCSI' reporting 'D.02'
#: IS accepted, while an IBM 'ULT3580-TD8' reporting 'D.02' is not: the same
#: revision, two answers, decided by the vendor id.
#:
#: This table was vendor-blind until 2026-09-30 and told operators that any LTO8
#: drive needed HB81. It was generalised from the one sample that hit the wall,
#: which happened to be an IBM drive.
#:
#: If upstream ever adds a second vendor's check, add a row here and nothing
#: else changes - which is why this is not named IBM_FIRMWARE_MINIMUM.
#:
#: Within IBM: compared as the four revision bytes read big-endian
#: (ibm_tape.c:1411), so the comparison is on the ASCII and not on a version
#: number - 'D.02' is 0x442E3032 and loses to 'HB81' at 0x48423831. ONLY these
#: three families have a minimum; LTO6, LTO7, LTO9, LTO10 and the
#: TS115x/116x/117x drives fall through to `default: break`
#: (ibm_tape.c:1435-1443) and are accepted with any revision at all.
FIRMWARE_MINIMUM: Dict[str, Dict[str, str]] = {
    'IBM': {
        'LTO5': 'B170',
        'LTO8': 'HB81',
        'TS1140': '3694',
    },
    # 'HP', 'HPE', 'QUANTUM': no gate. Deliberately absent rather than empty -
    # firmware_minimum_for() returns None for a vendor with no table.
}

#: product id -> drive family, per vendor id, from the TAPEDRIVE() tables in
#: src/tape_drivers/{ibm,hp,quantum}_tape.c. The family is the DRIVE_* name with
#: its prefix removed; a half-height drive shares its full-height sibling's
#: firmware minimum, which firmware_minimum_for() relies on.
SUPPORTED_DRIVES: Dict[str, Dict[str, str]] = {
    'IBM': {
        'ULT3580-TD5': 'LTO5',    'ULTRIUM-TD5': 'LTO5',
        'ULT3580-TD6': 'LTO6',    'ULTRIUM-TD6': 'LTO6',
        'ULT3580-TD7': 'LTO7',    'ULTRIUM-TD7': 'LTO7',
        'ULT3580-TD8': 'LTO8',    'ULTRIUM-TD8': 'LTO8',
        'ULT3580-TD9': 'LTO9',    'ULTRIUM-TD9': 'LTO9',
        'ULT3580-TDA': 'LTO10',   'ULTRIUM-TDA': 'LTO10',
        'ULT3580-HH5': 'LTO5_HH', 'ULTRIUM-HH5': 'LTO5_HH',
        'ULT3580-HH6': 'LTO6_HH', 'ULTRIUM-HH6': 'LTO6_HH',
        'ULT3580-HH7': 'LTO7_HH', 'ULTRIUM-HH7': 'LTO7_HH',
        'ULT3580-HH8': 'LTO8_HH', 'ULTRIUM-HH8': 'LTO8_HH',
        'ULT3580-HH9': 'LTO9_HH', 'ULTRIUM-HH9': 'LTO9_HH',
        'ULT3580-HHA': 'LTO10_HH', 'ULTRIUM-HHA': 'LTO10_HH',
        'HH LTO Gen 5': 'LTO5_HH', 'HH LTO Gen 6': 'LTO6_HH',
        'HH LTO Gen 7': 'LTO7_HH', 'HH LTO Gen 8': 'LTO8_HH',
        'HH LTO Gen 9': 'LTO9_HH',
        '03592E07': 'TS1140',     '03592E08': 'TS1150',
        '0359255E': 'TS1155',     '0359255F': 'TS1155',
        '0359260E': 'TS1160',     '0359260F': 'TS1160',
        '0359260S': 'TS1160',
        '0359270F': 'TS1170',     '0359270S': 'TS1170',
    },
    'HP': {
        'Ultrium 5-SCSI': 'LTO5',
        'Ultrium 6-SCSI': 'LTO6',
        'Ultrium 7-SCSI': 'LTO7',
    },
    'HPE': {
        'Ultrium 8-SCSI': 'LTO8',
        'Ultrium 9-SCSI': 'LTO9',
    },
    'QUANTUM': {
        'ULTRIUM 5': 'LTO5_HH',
        'ULTRIUM 6': 'LTO6_HH',
        'ULTRIUM-HH5': 'LTO5_HH',
        'ULTRIUM-HH6': 'LTO6_HH',
        'ULTRIUM-HH7': 'LTO7_HH',
        'ULTRIUM-HH8': 'LTO8_HH',
        'ULTRIUM-HH9': 'LTO9_HH',
    },
}


def firmware_minimum_for(family: Optional[str],
                         vendor: Optional[str]) -> Optional[str]:
    """The revision a drive of this vendor and family must report.

    None when any revision will do - which is every HP, HPE and Quantum drive,
    because the firmware check is dispatched on the vendor and only IBM has one
    (vendor_compat.c:347). vendor is required, not optional: making it default to
    anything is how this function came to apply IBM's minimums to everybody.

    A half-height drive shares its sibling's minimum: LTO8_HH is gated by the
    same 'HB81' as LTO8, because ibm_tape.c lists both in one case.
    """
    if not family:
        return None
    for_vendor = FIRMWARE_MINIMUM.get((vendor or '').strip())
    if not for_vendor:
        return None
    return for_vendor.get(family.replace('_HH', ''))


def _revision_is_recent_enough(revision: str, minimum: str) -> bool:
    """LTFS's comparison, not a version comparison.

    ltfs_betou32() reads four bytes big-endian, so this is a comparison of the
    first four characters as a big-endian integer - which for printable ASCII is
    the same as comparing the strings, padded to four.
    """
    return revision.ljust(4)[:4] >= minimum.ljust(4)[:4]


@dataclass
class LtfsSupport:
    """Whether LTFS would open one drive, and why not when it would not."""
    supported: bool
    #: 'LTO8', 'TS1140', 'LTO8_HH' ... None when the product is unknown.
    family: Optional[str] = None
    #: The minimum this family is gated on, when there is one.
    firmware_minimum: Optional[str] = None
    #: One short sentence, suitable for a CLI column or a tooltip.
    reason: str = ''
    #: True when the vendor id is one LTFS does not know. Kept separate because
    #: it is the case that used to crash LTFS outright, and an operator can fix
    #: it by choosing a different profile.
    vendor_unknown: bool = False

    def to_dict(self) -> Dict:
        return {
            'supported': self.supported,
            'family': self.family,
            'firmware_minimum': self.firmware_minimum,
            'reason': self.reason,
            'vendor_unknown': self.vendor_unknown,
        }


def supports(vendor: str, product: str,
             revision: Optional[str] = None) -> LtfsSupport:
    """Would LTFS open a drive presenting these strings?

    vendor and product are taken as device.conf spells them; trailing padding is
    ignored, because that is what LTFS's strncmp comparisons do. revision is
    optional: without it the firmware gate is reported but not applied, which is
    what a catalogue listing wants - the model is capable, this particular drive
    may not be.
    """
    vendor = (vendor or '').strip()
    product = (product or '').strip()

    if vendor not in KNOWN_VENDORS:
        note = (' It is defined in LTFS but unreachable, because get_vendor_id()'
                ' has no branch for it.' if vendor in UNREACHABLE_VENDORS else '')
        return LtfsSupport(
            supported=False, vendor_unknown=True,
            reason=f'LTFS does not know the vendor id {vendor!r}.{note}')

    family = SUPPORTED_DRIVES[vendor].get(product)
    if not family:
        return LtfsSupport(
            supported=False,
            reason=f'LTFS has no {vendor} drive with product id {product!r}.')

    minimum = firmware_minimum_for(family, vendor)
    if minimum and revision is not None:
        if not _revision_is_recent_enough(revision.strip(), minimum):
            return LtfsSupport(
                supported=False, family=family, firmware_minimum=minimum,
                reason=(f'{family} needs firmware {minimum} or later; this drive '
                        f'reports {revision.strip()!r}.'))

    return LtfsSupport(supported=True, family=family, firmware_minimum=minimum,
                       reason='')


def usable_drive_models(vendor: str) -> Dict[str, str]:
    """Every product id LTFS would open for this vendor, product -> family.

    Empty for a vendor LTFS does not know, which is the honest answer rather
    than an error: there is nothing wrong with the vendor, LTFS just has no
    table for it.
    """
    return dict(SUPPORTED_DRIVES.get((vendor or '').strip(), {}))

# -- which cartridges could hold an LTFS volume in a given drive -------------
#
# Three conditions have to hold at once, and the middle one is the one that gets
# forgotten:
#
#   1. the generation can be partitioned      LTO-5 introduced it
#   2. the DRIVE CAN WRITE IT, not merely load it - mkltfs writes, so a
#      read-only combination is no use whatever the medium supports
#   3. a barcode can name it                  SUFFIX_BY_DENSITY
#
# Deliberately NOT asked here: whether LTFS will open the drive at all. That is
# supports() above, and a caller checks both - one question per function. This
# answers "if LTFS can drive this model, which cartridges could it format".

#: LTO-5 was the first generation whose media can be partitioned, and an LTFS
#: volume needs two partitions - so nothing older can hold one, in any drive.
#: The same constant and the same reason as tapes/ltfs_state.py's gate.
FIRST_PARTITIONABLE_LTO = 5

#: IBM 3592 densities, which are partitionable and which LTFS does support: its
#: drive tables carry TS1140 (03592E07) and later, and IBM ships LTFS for them.
#: Kept as a set rather than folded into the LTO rule because the generation is
#: not in the name and there is no arithmetic to do.
JAGUAR_DENSITIES = frozenset({'J1A', 'E05', 'E06', 'E07'})


@dataclass
class LtfsMedium:
    """One density, and whether a cartridge of it could become an LTFS volume
    in the drive that was asked about."""
    density: str
    #: The barcode suffix that names it - 'L8' for LTO8.
    suffix: str
    usable: bool
    #: Why not, when it is not. Empty when it is.
    reason: str = ''

    def to_dict(self) -> Dict:
        return {'density': self.density, 'suffix': self.suffix,
                'usable': self.usable, 'reason': self.reason}


def _lto_generation(density: str) -> Optional[int]:
    """5 from 'LTO5', 10 from 'LTO10' and 'LTO10P', None for anything else."""
    if not density.startswith('LTO'):
        return None
    digits = ''.join(c for c in density[3:] if c.isdigit())
    return int(digits) if digits else None


def ltfs_media_for(product: str) -> List[LtfsMedium]:
    """Every density this drive model handles, said to be usable for LTFS or not.

    Returns the whole list rather than only the usable part, because a caller
    offering a choice should be able to say why a density is missing - "the
    drive cannot write LTO5" is a better thing for an operator to read than a
    density that is simply absent.

    Native density first, the order personalities lists them in, so the first
    usable entry is the natural default.

    Empty for a drive model MHVTL does not recognise: it emulates a generic
    drive with no media list at all, and inventing one here would be a guess.
    """
    support = personalities.drive_media(product)
    if support is None:
        return []

    writable = set(support.read_write)
    media: List[LtfsMedium] = []
    for density in list(support.read_write) + list(support.read_only):
        suffix = personalities.SUFFIX_BY_DENSITY.get(density)
        if suffix is None:
            # Nothing can create it, because no barcode names it.
            media.append(LtfsMedium(density, '', False,
                                    'no barcode suffix names this density'))
            continue

        generation = _lto_generation(density)
        if generation is not None and generation < FIRST_PARTITIONABLE_LTO:
            media.append(LtfsMedium(
                density, suffix, False,
                f'LTO-{generation} media cannot be partitioned; LTFS needs two '
                f'partitions, which LTO-{FIRST_PARTITIONABLE_LTO} introduced'))
            continue
        if generation is None and density not in JAGUAR_DENSITIES:
            # AIT, DLT, SDLT, 9840, 9940, T10000: no LTFS drive table covers
            # the drives that write them, so this should be unreachable. Said
            # rather than assumed, because a silent True here would be a claim.
            media.append(LtfsMedium(
                density, suffix, False,
                'not an LTO or 3592 density; LTFS partitioning is not '
                'established for it'))
            continue

        if density not in writable:
            media.append(LtfsMedium(
                density, suffix, False,
                f'this drive loads {density} read-only, and mkltfs writes'))
            continue

        media.append(LtfsMedium(density, suffix, True))
    return media


def ltfs_densities_for(product: str) -> List[str]:
    """Just the usable densities, native first. The default is the first."""
    return [m.density for m in ltfs_media_for(product) if m.usable]
