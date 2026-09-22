"""Can this cartridge go in that drive?

Moved from tape_operations_service.py (LTO_COMPATIBILITY, DRIVE_MODEL_TO_LTO,
BARCODE_SUFFIX_TO_LTO, get_lto_from_barcode, check_tape_drive_compatibility).

The LTO rule, as MHVTL 1.8 emulates it - and it is not the one-rule-fits-all
"reads two back, writes one back" this module used to state:

    LTO-1         reads and writes LTO-1
    LTO-2         reads and writes LTO-2 and LTO-1
    LTO-3 .. 7    reads and writes n and n-1; reads n-2
    LTO-8         reads and writes LTO-8 and LTO-7 only
    LTO-9         reads and writes LTO-9 and LTO-8 only
    LTO-10        reads and writes LTO-10 (and LTO-10 Premium) only

The table used to have LTO-8 reading LTO-6 and LTO-9 reading LTO-7, so the
operator page offered mounts MHVTL refuses. Transcribed from the drive
personalities in usr/pm/ult3580_pm.c and usr/pm/hp_ultrium_pm.c, and checked
against them by tests/test_personalities.

Two generations of the same number are not always the same cartridge. A WORM
cartridge carries a different suffix (LU is LTO-4 WORM) but the same generation
for compatibility, because the drive's read and write capability does not change
- what changes is whether the medium can be overwritten, which is the tape's
property and not the drive's.

The LTO table is the one with generation-aware messages ("too new", "too
old"). verdict() is what callers use: it answers for any cartridge and any
drive, LTO or not, from profiles/personalities.DRIVE_MEDIA, and falls back to
"cannot tell" - never to a refusal - when the barcode or the product string is
one MHVTL's tables do not name.

Why the table is not in profiles/: profiles answer "what may an operator configure",
from vendor tables. This answers "what will the hardware do with what is already
in the slots", which is asked of libraries that were configured elsewhere.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Dict, List, Optional

from ..profiles import personalities

logger = logging.getLogger(__name__)

#: drive generation -> what it can read, and what it can write.
LTO_COMPATIBILITY = {
    'LTO-10': {'read': ['LTO-10'], 'write': ['LTO-10']},
    'LTO-9': {'read': ['LTO-9', 'LTO-8'], 'write': ['LTO-9', 'LTO-8']},
    'LTO-8': {'read': ['LTO-8', 'LTO-7'], 'write': ['LTO-8', 'LTO-7']},
    'LTO-7': {'read': ['LTO-7', 'LTO-6', 'LTO-5'], 'write': ['LTO-7', 'LTO-6']},
    'LTO-6': {'read': ['LTO-6', 'LTO-5', 'LTO-4'], 'write': ['LTO-6', 'LTO-5']},
    'LTO-5': {'read': ['LTO-5', 'LTO-4', 'LTO-3'], 'write': ['LTO-5', 'LTO-4']},
    'LTO-4': {'read': ['LTO-4', 'LTO-3', 'LTO-2'], 'write': ['LTO-4', 'LTO-3']},
    'LTO-3': {'read': ['LTO-3', 'LTO-2', 'LTO-1'], 'write': ['LTO-3', 'LTO-2']},
    'LTO-2': {'read': ['LTO-2', 'LTO-1'], 'write': ['LTO-2', 'LTO-1']},
    'LTO-1': {'read': ['LTO-1'], 'write': ['LTO-1']},
}

#: The product identification string a drive reports, by generation. This is
#: what device.conf carries and what SCSI inquiry returns, so it is how a drive
#: is recognised without asking the daemon anything.
DRIVE_MODEL_TO_LTO = {
    **{f'ULT3580-TD{g}': f'LTO-{g}' for g in range(1, 10)},
    'ULT3580-TDA': 'LTO-10',
    **{f'ULT3580-HH{g}': f'LTO-{g}' for g in range(7, 10)},
    'ULT3580-HHA': 'LTO-10',
    **{f'ULTRIUM-TD{g}': f'LTO-{g}' for g in range(1, 10)},
    'ULTRIUM-TDA': 'LTO-10',
    **{f'ULTRIUM-HH{g}': f'LTO-{g}' for g in range(2, 10)},
    'ULTRIUM-HHA': 'LTO-10',
    **{f'Ultrium {g}-SCSI': f'LTO-{g}' for g in range(1, 9)},
}

#: The two-character barcode suffix, by generation. The W* entries are WORM
#: cartridges: same generation for read and write capability, different medium.
BARCODE_SUFFIX_TO_LTO = {
    'L1': 'LTO-1', 'L2': 'LTO-2', 'L3': 'LTO-3', 'L4': 'LTO-4', 'L5': 'LTO-5',
    'L6': 'LTO-6', 'L7': 'LTO-7', 'L8': 'LTO-8', 'L9': 'LTO-9',
    'LT': 'LTO-3', 'LU': 'LTO-4', 'LV': 'LTO-5', 'LW': 'LTO-6',
    'LX': 'LTO-7', 'LY': 'LTO-8', 'LZ': 'LTO-9',
    # LTO-10: LA data, LH WORM, PA Premium (etc/generate_library_contents.in:74)
    'LA': 'LTO-10', 'LH': 'LTO-10', 'PA': 'LTO-10',
}


def lto_for_barcode(barcode: str) -> Optional[str]:
    """The LTO generation a barcode's suffix declares, or None.

    None for a cartridge that is not LTO at all - a T10000 or 3592 barcode is
    perfectly valid and simply has no LTO generation. Callers show "unknown"
    rather than refusing the tape.
    """
    if not barcode or len(barcode) < 2:
        return None
    return BARCODE_SUFFIX_TO_LTO.get(barcode.strip().upper()[-2:])


def lto_for_drive_model(model: str) -> Optional[str]:
    """The LTO generation a drive's product identification declares, or None."""
    return DRIVE_MODEL_TO_LTO.get((model or '').strip())


def check(tape_lto: str, drive_lto: str) -> Dict:
    """Whether a cartridge can be read and written in a drive.

    Returns compatible/can_read/can_write and a message written for an
    operator: "too new for" and "too old for" are the two ways this fails, and
    they need different actions, so the message says which.
    """
    if not tape_lto or not drive_lto:
        return {'compatible': False, 'can_read': False, 'can_write': False,
                'message': 'Unknown tape or drive generation'}

    capability = LTO_COMPATIBILITY.get(drive_lto)
    if capability is None:
        return {'compatible': False, 'can_read': False, 'can_write': False,
                'message': f'Unknown drive generation: {drive_lto}'}

    can_read = tape_lto in capability['read']
    can_write = tape_lto in capability['write']

    if can_read and can_write:
        message = f'{tape_lto} tape reads and writes in a {drive_lto} drive'
    elif can_read:
        message = (f'{tape_lto} tape is read-only in a {drive_lto} drive; '
                   f'a restore will work, a backup will not')
    else:
        message = _incompatible_message(tape_lto, drive_lto)

    return {'compatible': can_read, 'can_read': can_read, 'can_write': can_write,
            'message': message}


def _incompatible_message(tape_lto: str, drive_lto: str) -> str:
    """Say which way round the mismatch is; the fix differs."""
    tape_gen, drive_gen = _generation(tape_lto), _generation(drive_lto)
    if tape_gen and drive_gen and tape_gen > drive_gen:
        return (f'{tape_lto} tape is too new for a {drive_lto} drive; '
                f'it needs a {tape_lto} drive or newer')
    if tape_gen and drive_gen:
        return (f'{tape_lto} tape is too old for a {drive_lto} drive, which '
                f'reads no further back than '
                f'{LTO_COMPATIBILITY[drive_lto]["read"][-1]}')
    return f'{tape_lto} tape is not compatible with a {drive_lto} drive'


def _generation(lto: str) -> Optional[int]:
    """8 from "LTO-8"."""
    try:
        return int((lto or '').split('-')[1])
    except (IndexError, ValueError):
        return None


def drives_for_tape(tape_lto: str, drives: List[Dict]) -> List[Dict]:
    """Which of these drives could take this cartridge.

    Each drive is a dict with at least lto_generation and drive_num; only
    drives with nothing loaded are offered, because a mount into a full drive
    fails whatever the generations are.
    """
    usable = []
    for drive in drives:
        if drive.get('full'):
            continue
        result = check(tape_lto, drive.get('lto_generation'))
        if result['compatible']:
            usable.append({'drive_num': drive.get('drive_num'),
                           'lto_generation': drive.get('lto_generation'),
                           'can_write': result['can_write']})
    return usable


def verdict(barcode: str, drive_model: str) -> Dict:
    """Can the cartridge with this barcode go into a drive with this product?

    The one question the mount page, the mount service and the CLI all ask.
    Returns known/compatible/can_read/can_write/message, plus what was
    recognised (tape_density, tape_lto, drive_lto) so a page can show it.

    compatible is False only when MHVTL's own tables say the drive refuses the
    cartridge - which it does by accepting the move and then unloading the
    tape in the drive (usr/cmd/vtltape.c:1512), leaving it for someone to
    unmount. An unrecognised barcode or drive is compatible and known=False.
    """
    barcode = (barcode or '').strip().upper()
    density = personalities.density_for_barcode(barcode)
    tape_lto = lto_for_barcode(barcode)
    drive_lto = lto_for_drive_model(drive_model)
    found = {'tape_density': density, 'tape_lto': tape_lto,
             'drive_model': drive_model, 'drive_lto': drive_lto}

    if barcode.startswith('CLN'):
        # Cleaning cartridges are loaded read-only by the drive family they
        # belong to; the drive decides, and a mount is how cleaning starts.
        return {**found, 'known': False, 'compatible': True, 'can_read': True,
                'can_write': False,
                'message': 'Cleaning cartridge; the drive decides whether it accepts it'}

    if tape_lto and drive_lto:
        return {**found, 'known': True, **check(tape_lto, drive_lto)}

    media = personalities.media_verdict(density, drive_model)
    if not media['known']:
        return {**found, 'known': False, 'compatible': True, 'can_read': True,
                'can_write': True,
                'message': ('Cannot tell from the barcode and the drive model '
                            'whether this cartridge loads; MHVTL decides when '
                            'it is mounted')}

    can_read, can_write = media['can_read'], media['can_write']
    if can_read and can_write:
        message = f'{density} cartridge reads and writes in a {drive_model} drive'
    elif can_read:
        message = (f'{density} cartridge is read-only in a {drive_model} drive; '
                   f'a restore will work, a backup will not')
    else:
        loads = personalities.drive_media(drive_model).loads
        message = (f'A {drive_model} drive does not load {density} cartridges; '
                   f'it takes {", ".join(loads)}')
    return {**found, 'known': True, 'compatible': can_read,
            'can_read': can_read, 'can_write': can_write, 'message': message}


def mount_matrix(slots: List[Dict], drives: List[Dict]) -> Dict[int, Dict[int, Dict]]:
    """{slot_num: {drive_num: verdict}} for every loaded slot and every drive.

    Slots and drives are the mtx status dicts, with the drive's product string
    under 'model'. A loaded drive still gets a verdict - the page shows why it
    is not offered separately from whether the tape would fit.
    """
    return {
        slot['slot_num']: {drive['drive_num']: verdict(slot.get('barcode'),
                                                       drive.get('model'))
                           for drive in drives}
        for slot in slots if slot.get('full') and slot.get('barcode')}
