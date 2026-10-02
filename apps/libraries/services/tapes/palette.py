"""Which colour a tape generation is drawn in. One table, both front ends.

THIS IS THE ONLY COPY OF THIS RULE. The mount page used to work it out in
JavaScript - twice, in two functions that disagreed with each other and with
the Python one, so an LTO-9 drive badge was painted with the LTO-8 colour and
an LTO-4 cartridge was given a class no stylesheet defines. See
docs/sphinx/guides/architecture.rst, "Presentation decisions - who gets to pick
a colour".

A token, not a class
--------------------
What leaves this module is a TOKEN: a stable identifier for a generation. It
is spelled `lto-8` so the stylesheet needs no translation table, but the
direction matters - the CSS class is named after the token, not the token after
the class. A terminal renders the same token through its own table
(mhvtl_cli/colour.py). A service does not know what a class is.

The colours are the real cartridge shell colours, tailored
----------------------------------------------------------
The `hue` column is what the physical cartridge actually looks like, from the
Linear Tape-Open cartridge specification table - the *typical* column, which is
what IBM, Quantum and Fujifilm ship. We follow that one because every drive
MHVTL emulates for us is an IBM ULT3580/ULTRIUM or a Quantum/STK part. HP ships
a different palette entirely and is not followed.

They cannot be used literally, because they repeat on a five-generation cycle:

    LTO-6 and LTO-8 are BOTH "Dark red".
    LTO-5 and LTO-9 are BOTH "Green (dark)".
    LTO-1, LTO-2, LTO-7 and LTO-10 are ALL "Black".

Library 10 holds LTO-8 and LTO-6 cartridges and LTO-8 and LTO-6 drives, so the
first collision is not hypothetical. Each generation therefore keeps its real
hue and is separated inside that family by lightness - older darker, newer
lighter - which also puts the generations in use at the readable end.

COLOUR IS A HINT, NEVER THE IDENTIFIER. Because the real colours collide, every
caller prints the generation as text beside the colour. That is what makes
grading the black family acceptable rather than a lie, and it is what keeps the
map usable in a dim terminal, under --no-colour, for a colour-blind operator
and on a printed page.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
      (this module is reference data, like barcodes and compatibility: it
      returns plain values, and TapeService.palette() wraps them)
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from typing import Dict, List, Optional

from . import compatibility

#: A generation that is not LTO at all - a T10000 or a 3592 - and a cartridge
#: whose suffix names nothing. It is given no colour rather than a bad one.
UNKNOWN = 'lto-unknown'

#: generation -> token, real shell hue, ANSI 256 index, dark-theme hex,
#: light-theme hex, and what separates it from the generation it collides with.
#: Every generation the catalogue can emit has an entry. There is no bucketing:
#: LTO-4 does not borrow LTO-5's colour and LTO-10 is not "unknown", which were
#: both true of the table this replaces.
PALETTE = {
    'LTO-1':  ('lto-1',  'Black',        238, '#3f3f46', '#52525b', 'darkest grey'),
    'LTO-2':  ('lto-2',  'Black',        244, '#52525b', '#71717a', 'grey'),
    'LTO-3':  ('lto-3',  'Purple',        93, '#7e22ce', '#6b21a8', 'only purple'),
    'LTO-4':  ('lto-4',  'Blue-gray',     67, '#64748b', '#475569', 'only slate'),
    'LTO-5':  ('lto-5',  'Green (dark)',  28, '#15803d', '#166534', 'deep green'),
    'LTO-6':  ('lto-6',  'Dark red',     124, '#9f1239', '#881337', 'deep red'),
    'LTO-7':  ('lto-7',  'Black',        250, '#a1a1aa', '#3f3f46', 'light graphite'),
    'LTO-8':  ('lto-8',  'Dark red',     196, '#e11d48', '#be123c', 'bright red'),
    'LTO-9':  ('lto-9',  'Green (dark)',  41, '#22c55e', '#15803d', 'bright green'),
    'LTO-10': ('lto-10', 'Black',        255, '#d4d4d8', '#27272a', 'near-white'),
}

#: The unknown entry, kept out of PALETTE because it is not a generation.
UNKNOWN_ENTRY = (UNKNOWN, '-', 240, 'dim', 'dim', 'no colour at all')

#: token -> generation, for a caller holding only the token.
GENERATION_BY_TOKEN = {entry[0]: gen for gen, entry in PALETTE.items()}

#: What a cartridge is, as far as LTFS is concerned. '' is NOT_ASKED: the
#: cartridge was never read, which is not the same as "not an LTFS volume".
LTFS_MARKS = {'ltfs': 'ltfs', 'was': 'ltfs-was'}


def token_for_generation(generation: Optional[str]) -> str:
    """'LTO-8' -> 'lto-8'. Anything unrecognised -> 'lto-unknown'."""
    entry = PALETTE.get((generation or '').strip().upper())
    return entry[0] if entry else UNKNOWN


def token_for_tape(barcode: str = None, density: str = None) -> str:
    """Rule 1: a cartridge is coloured by its LTO generation.

    From the barcode suffix, which is where the generation is declared. The
    density is a fallback for a caller that has one and no barcode; 'LTO8' and
    'LTO-8' are both accepted.
    """
    if barcode:
        return token_for_generation(compatibility.lto_for_barcode(barcode))
    if density:
        text = density.strip().upper().replace('LTO-', 'LTO')
        if text.startswith('LTO'):
            return token_for_generation('LTO-' + text[3:])
    return UNKNOWN


def highest_supported(model: str) -> Optional[str]:
    """The newest generation a drive can read, from LTO_COMPATIBILITY.

    Worth knowing: with that table as it stands this is always the drive's own
    generation - in all ten rows the highest readable and highest writable
    entry are both it. It is still computed rather than assumed, because the
    rule is the rule and the table is the thing that may change.
    """
    own = compatibility.lto_for_drive_model(model)
    if not own:
        return None
    supported = compatibility.LTO_COMPATIBILITY.get(own, {}).get('read') or [own]
    return max(supported, key=_number)


def token_for_drive(model: str, loaded_barcode: str = None) -> str:
    """Rules 2 and 3, in that order.

    Rule 2: a drive holding a cartridge is coloured by THAT CARTRIDGE. The
    drive shows what is in it - an LTO-7 cartridge in an LTO-8 drive makes the
    drive LTO-7 coloured, because the thing an operator is looking for is the
    tape.

    Rule 3: an empty drive is coloured by the highest generation it supports,
    so the colour then says what the drive is for.
    """
    if loaded_barcode:
        return token_for_tape(loaded_barcode)
    return token_for_generation(highest_supported(model))


def mark_for_cartridge(ltfs, ltfs_was) -> str:
    """'ltfs', 'ltfs-was' or '' - a MARK, never a colour.

    LTFS is something a cartridge is AS WELL AS its generation, so it never
    takes a fill of its own. `None` means the cartridge was not read, and gets
    no mark: "we did not look" is not "it is not one".
    """
    if ltfs is True:
        return LTFS_MARKS['ltfs']
    if ltfs_was is True:
        return LTFS_MARKS['was']
    return ''


def label_for(token: str) -> str:
    """'lto-8' -> 'LTO-8'. The unknown entry reads 'not LTO'.

    It is not a generation called "unknown": a T10000 or a 3592 has no LTO
    generation at all, and saying so is different from saying we could not
    read one.
    """
    if token == UNKNOWN:
        return 'not LTO'
    generation = GENERATION_BY_TOKEN.get(token)
    return generation or token


def present_in(tokens) -> List[Dict]:
    """The generations a caller is holding, in order, each with its label.

    For a legend. All ten would be a wall of colour for a library with two,
    and a legend is for reading - so the SERVICE decides which to list and the
    page renders what it is given, the same way `mhvtl op layout` does.
    """
    seen = []
    for token in tokens:
        if token and token not in seen:
            seen.append(token)
    seen.sort(key=lambda t: _number(GENERATION_BY_TOKEN.get(t, '')) or 99)
    return [{'token': t, 'label': label_for(t)} for t in seen]


def rows(include_unknown: bool = True) -> List[Dict]:
    """The whole palette, in generation order, ready to print or render."""
    entries = [(gen, PALETTE[gen]) for gen in sorted(PALETTE, key=_number)]
    if include_unknown:
        entries.append(('', UNKNOWN_ENTRY))
    return [{'token': e[0], 'generation': gen, 'hue': e[1], 'ansi': e[2],
             'dark': e[3], 'light': e[4], 'separated_by': e[5]}
            for gen, e in entries]


def collisions() -> List[Dict]:
    """The generations the real shell colours cannot tell apart.

    Not a curiosity: it is the reason the palette is tailored at all, and a
    caller showing the legend should be able to say so without holding its own
    copy of the fact.
    """
    by_hue = {}
    for gen in sorted(PALETTE, key=_number):
        token, hue = PALETTE[gen][0], PALETTE[gen][1]
        by_hue.setdefault(hue, []).append({'generation': gen, 'token': token,
                                           'separated_by': PALETTE[gen][5]})
    return [{'hue': hue, 'generations': members}
            for hue, members in by_hue.items() if len(members) > 1]


def _number(generation: str) -> int:
    """LTO-10 sorts after LTO-9, which a string sort does not do."""
    try:
        return int(generation.split('-')[1])
    except (IndexError, ValueError):
        return 0
