"""Barcodes, prefixes and media densities.

Gathers what was scattered across tape_operations_service.py:244
(get_density_suffix_mapping), :290 (get_lto_from_barcode), :1603
(get_existing_barcodes), the prefix detection at :1539, and the eleven-line
prefix block duplicated verbatim in tape_operations_views.py:747-760 and
:852-865.

Validation lives here because of where a barcode ends up. It is interpolated
into `sudo mktape -m <barcode>` and into `sudo rm -rf <media>/<barcode>`, and the
only check before this was `len(barcode) >= 4`. A barcode of '../..' was a
working path traversal into a root-owned delete. One rule, one place:

    VALID_BARCODE = ^[A-Z0-9]{1,16}$

That is the SMC physical cartridge label charset, and mktape refuses more than 16
characters anyway.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import re
from typing import Dict, List, Optional, Set, Tuple

#: The only shape a barcode may take. Checked before the value reaches any
#: command line or path.
BARCODE_RE = re.compile(r'^[A-Z0-9]{1,16}$')
MAX_BARCODE_LENGTH = 16

#: Barcode suffix to density, and back. One table, in profiles/personalities,
#: taken from MHVTL's make_vtl_media; this module used to carry its own copy,
#: which named T10K media right but had no 9840, 9940, DLT, SDLT220/320, AIT1-3
#: or E07 suffixes, so those barcodes were created as the default density.
from ..profiles.personalities import DENSITY_BY_SUFFIX, SUFFIX_BY_DENSITY  # noqa: E402

#: Leading characters MHVTL reads as a media kind rather than as data.
CLEANING_PREFIX = 'CLN'
WORM_PREFIX = 'W'

#: How many characters of a barcode are the library's prefix.
PREFIX_LENGTH = 3


class InvalidBarcode(ValueError):
    """The barcode is not a shape we are willing to put on a command line."""


def is_valid(barcode: str) -> bool:
    return bool(barcode) and bool(BARCODE_RE.match(barcode))


def validate(barcode: str) -> str:
    """Return the barcode, or raise InvalidBarcode saying why it was refused."""
    if not barcode:
        raise InvalidBarcode('a barcode is required')
    if len(barcode) > MAX_BARCODE_LENGTH:
        raise InvalidBarcode(
            f'{barcode!r} is {len(barcode)} characters; the limit is '
            f'{MAX_BARCODE_LENGTH}')
    if not BARCODE_RE.match(barcode):
        raise InvalidBarcode(
            f'{barcode!r} contains characters a barcode may not: only A-Z and '
            f'0-9 are allowed')
    return barcode


def kind(barcode: str) -> str:
    """data, clean or WORM, as MHVTL reads the leading characters."""
    if barcode.startswith(CLEANING_PREFIX):
        return 'clean'
    if barcode.startswith(WORM_PREFIX):
        return 'WORM'
    return 'data'


def density_for(barcode: str) -> Optional[str]:
    """Density from the two-character suffix, or None if it is not one we know."""
    if not barcode or len(barcode) < 2:
        return None
    return DENSITY_BY_SUFFIX.get(barcode[-2:].upper())


#: Which palette entry a generation is drawn with. css/mhvtl-console.css
#: defines five, built from the theme's own tokens; anything older or
#: unrecognised shares the last one.
GENERATION_CLASS = {'9': 'lto-9', '8': 'lto-8', '7': 'lto-7', '6': 'lto-6',
                    '5': 'lto-5', '4': 'lto-5'}


def generation_class(density: str) -> str:
    """The CSS class for a density: LTO8 -> 'lto-8'.

    The mount page worked this out in JavaScript, which meant the rule lived
    in a browser and the tape tiles would have needed their own copy of it.
    """
    match = re.search(r'(\d+)', density or '')
    if not match:
        return 'lto-unknown'
    return GENERATION_CLASS.get(match.group(1), 'lto-unknown')


def suffix_for(density: str) -> Optional[str]:
    """The barcode suffix a density is written with."""
    return SUFFIX_BY_DENSITY.get((density or '').upper())


def split(barcode: str) -> Tuple[str, str, str]:
    """(prefix, number, suffix) - E01001L8 becomes ('E01', '001', 'L8')."""
    if len(barcode) < PREFIX_LENGTH + 2:
        return (barcode, '', '')
    return (barcode[:PREFIX_LENGTH], barcode[PREFIX_LENGTH:-2], barcode[-2:])


def build(prefix: str, number: int, suffix: str, *, digits: int = 3) -> str:
    """Assemble a barcode and check it before returning it."""
    return validate(f'{prefix}{number:0{digits}d}{suffix}'.upper())


def detect_prefix(barcodes: List[str], fallback: str = '') -> str:
    """The prefix a library is actually using.

    Read from the data tapes already in the library rather than computed from a
    brand table: a library whose database record disagrees with its media would
    otherwise get barcodes that do not match the ones already in its slots.
    Cleaning and WORM cartridges are skipped because their prefixes are fixed.
    """
    for barcode in barcodes:
        if kind(barcode) == 'data' and len(barcode) >= PREFIX_LENGTH:
            return barcode[:PREFIX_LENGTH]
    return fallback[:PREFIX_LENGTH] if fallback else ''


def detect_suffix(barcodes: List[str], fallback: str = 'L8') -> str:
    """The media suffix a library is actually using."""
    for barcode in barcodes:
        if kind(barcode) == 'data' and len(barcode) >= 2:
            return barcode[-2:]
    return fallback


def used_numbers(barcodes: List[str], prefix: str,
                 suffix: str = None) -> Set[int]:
    """The numbers already taken under a prefix.

    The suffix is not part of the match by default, because the number space is
    shared across tape generations: a library that already holds E01001L7 must
    not hand out E01001L8, which would be a second tape with the same number in
    the same library and an operator reading barcodes off a shelf cannot tell
    them apart. Pass a suffix only to ask about one generation specifically.
    """
    used = set()
    for barcode in barcodes:
        found_prefix, number, found_suffix = split(barcode)
        if found_prefix != prefix or not number.isdigit():
            continue
        if suffix is not None and found_suffix != suffix:
            continue
        used.add(int(number))
    return used


def next_number(barcodes: List[str], prefix: str, suffix: str = None) -> int:
    """The first unused number in a series, filling gaps before extending it.

    Filling gaps matters: a library that has had tapes deleted would otherwise
    keep counting upwards and leave holes no operator asked for.
    """
    used = used_numbers(barcodes, prefix, suffix=None)
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def consecutive_free(barcodes: List[str], prefix: str, start: int,
                     limit: int = 999) -> int:
    """How many numbers are free in a row from `start`.

    What a bulk-create form needs to know before offering to make N tapes.
    Capped at `limit`, and the cap is reported as the number itself: 999 means
    "999 or more", which is enough for any real library.
    """
    used = used_numbers(barcodes, prefix, suffix=None)
    count, candidate = 0, start
    while candidate not in used and count < limit:
        count += 1
        candidate += 1
    return count


def series(prefix: str, suffix: str, start: int, count: int,
           *, digits: int = 3) -> List[str]:
    """A run of consecutive barcodes, validated before any of them is used."""
    return [build(prefix, start + offset, suffix, digits=digits)
            for offset in range(count)]


#: The number field of each kind of cartridge, and so how many a library can
#: hold: data {prefix}{nnn}, cleaning CLN{lib}{n}, WORM W{brand}{lib}{nn}.
#: The formats are the ones the original bulk-create used and the create pages
#: still preview; the step 8 port had dropped them, so a "cleaning" run was
#: made with data barcodes, which MHVTL does not treat as cleaning cartridges.
KIND_DIGITS: Dict[str, int] = {'data': 3, 'clean': 1, 'WORM': 2}
MAX_PER_KIND: Dict[str, int] = {kind: 10 ** digits - 1
                                for kind, digits in KIND_DIGITS.items()}


def kind_head(kind_name: str, library_id: int, prefix: str = '') -> str:
    """Everything before the number, for a kind of cartridge.

        data    E01            E01001L8   (prefix is the library's data prefix)
        clean   CLN10          CLN101L8
        WORM    WE10           WE1001LY   (prefix's first letter is the brand)
    """
    if kind_name == 'clean':
        return f'{CLEANING_PREFIX}{int(library_id):02d}'
    if kind_name == 'WORM':
        return f'{WORM_PREFIX}{(prefix or "")[:1]}{int(library_id):02d}'
    return (prefix or '')[:PREFIX_LENGTH]


def numbers_under(barcodes: List[str], head: str, digits: int) -> Set[int]:
    """The numbers taken after `head`, whatever the suffix."""
    pattern = re.compile(rf'^{re.escape(head)}(\d{{{digits}}})[A-Z0-9]{{2}}$')
    return {int(match.group(1)) for match in map(pattern.match, barcodes) if match}


def first_free(barcodes: List[str], head: str, digits: int) -> int:
    """The first unused number after `head`, filling gaps first."""
    used = numbers_under(barcodes, head, digits)
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate
