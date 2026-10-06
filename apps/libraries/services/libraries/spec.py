"""Filling in a library specification from its vendor profile.

Moved from the first 80 lines of mhvtl_library_service.py:create_library, which
mixed three jobs in one block: applying profile defaults, checking the result,
and writing the files. Separating the first is what lets the CLI, the web form
and a test all start from the same filled-in specification, and lets the caller
show an operator what will be created before anything is written.

What the operator supplies is a profile key and an id. Everything else - the
vendor and product strings a backup application uses to recognise the device,
the drive model, the media type, the barcode series - has a profile answer, and
every one of them can be overridden by naming it explicitly.

Nothing here validates. apply_defaults fills gaps and raises only when the
profile itself is unknown, because a specification with no profile has no
defaults to apply. Everything else is validation.validate's job, which sees the
finished specification and so catches an override that contradicts the profile.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Any, Dict, List

from ..core import ValidationFailed
from ..profiles import personalities
from ..profiles.data import (PROFILES, get_default_drive_for_library,
                             get_default_media_for_drive, get_media_suffix,
                             get_profile)

logger = logging.getLogger(__name__)

class UnknownProfile(ValidationFailed):
    """The profile key names no vendor profile, so no defaults exist."""


def profile_key_of(library_spec: Dict[str, Any]):
    """The profile key under any of the three names the callers use.

    The web form posts 'profile', the older API passed 'vendor_profile' and the
    management command 'vendor_key'. Accepting all three here means nothing else
    has to know there was ever more than one.
    """
    return (library_spec.get('profile') or library_spec.get('vendor_profile')
            or library_spec.get('vendor_key'))


#: The serial rules, which are MHVTL's and live with its other limits in
#: profiles/personalities: how long a serial may be and why, how one is cut,
#: and the one convention this project composes them by. Imported rather than
#: copied - config/device_conf.py needs the same three, and config/ may not
#: import this package, so a copy here would be a second definition free to
#: drift. It had already happened: this module and device_conf.py each carried
#: `MAX_SERIAL_LENGTH = 10` and a private _truncate_serial.
MAX_SERIAL_LENGTH = personalities.MAX_SERIAL_LENGTH
device_serial = personalities.device_serial
truncate_serial = personalities.truncate_serial


def apply_defaults(library_spec: Dict[str, Any], *, next_id=None) -> Dict[str, Any]:
    """Return a copy of the specification with every profile default filled in.

    Args:
        library_spec: what the operator supplied. Not modified.
        next_id: called with no arguments to allocate an id when none is given.

    Raises:
        UnknownProfile: no profile key, or one no profile matches.
    """
    filled = dict(library_spec or {})
    key = profile_key_of(filled)

    if not key:
        raise UnknownProfile(
            'A vendor profile is required; valid profiles are: '
            + ', '.join(sorted(PROFILES)))
    try:
        profile = get_profile(key)
    except KeyError as exc:
        raise UnknownProfile(
            f'Unknown vendor profile {key!r}; valid profiles are: '
            + ', '.join(sorted(PROFILES))) from exc

    filled['profile'] = key

    if filled.get('library_id') is None:
        if next_id is None:
            raise UnknownProfile('library_id is required')
        filled['library_id'] = next_id()
    library_id = int(filled['library_id'])
    filled['library_id'] = library_id

    # Library identity. These reach SCSI inquiry, and backup software matches
    # on them, so a wrong default here is a library the application will not
    # drive rather than one that merely looks odd.
    filled.setdefault('vendor', profile.library_vendor)
    filled.setdefault('product', filled.get('library_model')
                      or profile.library_product_default)
    filled.setdefault('library_revision', profile.library_revision_default)
    filled['serial'] = truncate_serial(filled.get('serial')
                                       or device_serial(library_id))

    # What MHVTL will emulate for these strings. A default must fit the
    # model's element layout: the OVERLAND profile's default of four MAP slots
    # went into a layout with room for one, and MHVTL silently dropped three.
    # An explicit value over the limit is left for validation to refuse.
    layout = personalities.library_layout(filled['vendor'], filled['product'])

    # Drive identity.
    filled.setdefault('drive_vendor', profile.drive_vendor)
    # The model's own default drive, not the profile's: a profile has one
    # default, and it is not a drive every model of that vendor can carry.
    filled.setdefault('drive_product', filled.get('drive_model')
                      or _default_drive(key, filled['product'], profile))
    filled.setdefault('drive_revision', profile.drive_revision_default)

    # The per-slot list every writer reads. `drive` is the operator's own
    # list - a preset's [[name.drive]] array, or --drive MODEL:COUNT repeated
    # - and when it is given it decides how many drives there are; otherwise
    # one model fills num_drives slots. Either shape leaves here as
    # drive_slots, so nothing downstream has to ask which arrived.
    asked = _entries(filled.get('drive'), 'model')
    if asked:
        filled['num_drives'] = sum(count for _, count in asked)
    else:
        filled.setdefault('num_drives', min(profile.default_num_drives,
                                            layout.max_drives))
        asked = [(filled['drive_product'], int(filled['num_drives']))]
    filled['drive_slots'] = [{'vendor': filled['drive_vendor'],
                              'product': model,
                              'revision': filled['drive_revision']}
                             for model, count in asked
                             for _ in range(int(count))]

    # SCSI addressing. The target is allocated at write time against the
    # device.conf as it stands, so it is deliberately not defaulted here.
    filled.setdefault('channel', 0)
    filled.setdefault('lun', 0)

    # Media and barcodes. A mixed library's nominal density is the one
    # `mhvtl tape media` reports for a library that already exists - the
    # first density its drives can write, in slot order - so creation and
    # that command cannot disagree. See profiles/catalogue.
    asked_media = _entries(filled.get('media'), 'density')
    if asked_media:
        filled['media_count'] = sum(count for _, count in asked_media)
        filled.setdefault('media_type', _nominal_density(filled['drive_slots'],
                                                         asked_media))
    else:
        filled.setdefault('media_type', _default_media(key,
                                                       filled['drive_product'],
                                                       profile))
        filled.setdefault('media_count', min(profile.default_media_count,
                                             layout.max_slots))
        asked_media = [(filled['media_type'], int(filled['media_count']))]

    try:
        filled['media_suffix'] = get_media_suffix(filled['media_type'])
    except ValueError:
        # An unsupported media type is validation's to report, with the list of
        # what this profile does support. Leaving the suffix out is enough to
        # stop anything being written.
        filled.pop('media_suffix', None)

    # The runs library_contents is written from, in slot order. A density with
    # no suffix is left for validation to refuse: without one no barcode can
    # be built, so nothing can be written anyway.
    runs = []
    for density, count in asked_media:
        try:
            suffix = get_media_suffix(density)
        except ValueError:
            suffix = None
        runs.append({'density': density, 'suffix': suffix,
                     'count': int(count)})
    filled['media_runs'] = runs
    filled.setdefault('empty_slots', min(profile.default_empty_slots,
                                         layout.max_slots - filled['media_count']))
    filled.setdefault('map_count', min(profile.default_num_maps, layout.max_maps))
    filled.setdefault('barcode_prefix', f'{profile.barcode_leading}{library_id:02d}')

    return filled


def _entries(given, what: str):
    """[(name, count), ...] from a list of {what: name, count: n} tables.

    What a preset's ``[[name.drive]]`` array parses to, and what
    ``--drive MODEL:COUNT`` builds. Order is slot order and is kept exactly:
    the first entry fills slot 1, and a backup application that addresses a
    drive by its position notices when that moves.

    A missing count means one. Anything that is not a list of tables is
    ignored here and refused by validation, which can say so with the valid
    values beside it - this function has no way to report.
    """
    if not isinstance(given, (list, tuple)):
        return []
    entries = []
    for entry in given:
        if not isinstance(entry, dict) or not entry.get(what):
            continue
        try:
            count = int(entry.get('count', 1))
        except (TypeError, ValueError):
            continue
        if count > 0:
            entries.append((str(entry[what]), count))
    return entries


# -- what a specification asks for, whichever shape it asked in -------------
#
# A library's drives can be named four ways by the time anything reads them:
# as ``drive_slots``, once apply_defaults has run; as the operator's own
# ``drive`` list before it has, which is a preset's ``[[name.drive]]`` array
# and ``--drive MODEL:COUNT`` repeated; as the single ``drive_model`` and
# ``drives`` pair of a uniform library; or not at all, by a preset that has
# not got that far. Media is the same with ``media_runs``, ``media``,
# ``media_type`` and ``tapes``.
#
# These four functions are the only place that knows all of those, and
# everything that asks "what was asked for" asks them: validation, which must
# check a half-built preset against the models it really lists, and the
# interactive summary, which must print a mixed library as the two kinds it
# is. The checks read ``drive_model`` alone until 4 October 2026 - a mixed
# preset does not set it, so `preset set lib-ten --add-drive T10000C` was
# saved against a library model that cannot take one and refused later, at
# `library create`, with the preset already on disk.

def asked_drive_runs(library_spec: Dict[str, Any]):
    """[(model, count), ...] in slot order: the drives asked for.

    Consecutive slots of one model are one run again, so a filled
    specification and the list it was filled from give the same answer.
    """
    slots = library_spec.get('drive_slots')
    if slots:
        return _runs_of(slot.get('product') for slot in slots)
    entries = _entries(library_spec.get('drive'), 'model')
    if entries:
        return entries
    model = library_spec.get('drive_model') or library_spec.get('drive_product')
    count = _whole(library_spec.get('num_drives'))
    return [(model, count)] if model and count else []


def asked_media_runs(library_spec: Dict[str, Any]):
    """[(density, count), ...] in slot order: the cartridges asked for."""
    for key in ('media_runs', 'media'):
        entries = _entries(library_spec.get(key), 'density')
        if entries:
            return entries
    density = library_spec.get('media_type')
    count = _whole(library_spec.get('media_count'))
    return [(density, count)] if density and count is not None else []


def asked_drive_models(library_spec: Dict[str, Any]) -> List[str]:
    """Which drive models this specification asks for, each once, in order.

    Not the runs above with the counts dropped: a name needs no count, and a
    preset may well hold one without the other. `drive_model = "T10000C"`
    with no `drives` is a preset naming a drive its library model cannot take,
    and asking the runs for it answered "nothing was asked for" - so the
    checks had nothing to refuse.
    """
    slots = library_spec.get('drive_slots')
    names = ([slot.get('product') for slot in slots] if slots
             else [model for model, _
                   in _entries(library_spec.get('drive'), 'model')])
    if not names:
        names = [library_spec.get('drive_model')
                 or library_spec.get('drive_product')]
    return _each_once(names)


def asked_densities(library_spec: Dict[str, Any]) -> List[str]:
    """Which densities this specification asks for, each once, in order.

    The media half of asked_drive_models, and for the same reason: a preset
    may name a density before it says how many cartridges of it.
    """
    names = []
    for key in ('media_runs', 'media'):
        names = [density for density, _
                 in _entries(library_spec.get(key), 'density')]
        if names:
            break
    return _each_once(names or [library_spec.get('media_type')])


def asked_drive_count(library_spec: Dict[str, Any], default: int) -> int:
    """How many drives this specification asks for.

    A list decides the count - that is the whole of why `drives` beside a
    `drive` list is refused - then an explicit count, then the caller's
    default. Validation needs it to ask whether the drives fit the model's
    element layout and whether the host has enough SCSI targets left, and both
    questions are about the real number.
    """
    runs = asked_drive_runs(library_spec)
    if runs:
        return sum(count for _, count in runs)
    count = _whole(library_spec.get('num_drives'))
    return default if count is None else count


def asked_media_count(library_spec: Dict[str, Any], default: int) -> int:
    """How many cartridges this specification asks for. See
    asked_drive_count."""
    runs = asked_media_runs(library_spec)
    if runs:
        return sum(count for _, count in runs)
    count = _whole(library_spec.get('media_count'))
    return default if count is None else count


def parse_runs(given, what: str, *, least: int = 1) -> List[Dict[str, Any]]:
    """``['ULT3580-TD8:2', 'ULT3580-TD6']`` -> the list a specification takes.

    ``[{what: name, 'count': n}, ...]`` in the order given, which is slot
    order. A name with no count means one, so two names with no counts are
    two drives.

    ``least`` is how few a run may ask for, and it differs by caller rather
    than by kind. ``--media LTO8:0`` on the command line is a run that
    creates nothing and is refused; a cartridge row the setup form has just
    added holds nothing *yet*, and refusing it would mean the row could not
    survive the next change. Drives are always at least one: a row for no
    drives is not a kind of drive.

    ONE SYNTAX, TWO FRONT ENDS
    --------------------------
    ``MODEL:COUNT`` is what ``--drive`` takes on the command line and what the
    setup form's rows send back to the server when a row changes. It lived in
    ``mhvtl_cli/runs.py`` while the command line was the only caller, and the
    web cannot import that - so the query string would have needed a second
    encoding of the same thing, free to disagree with the first. It is here
    now, beside the readers that already understand this shape, and
    ``mhvtl_cli/runs.py`` keeps what is genuinely the command line's: the flag
    names, the metavars and the wording of the refusals.

    Raises ValueError. What to *do* about an unreadable run depends on how it
    arrived - a flag, a query string - so the caller says that, not this.
    """
    runs = []
    for item in given:
        name, _, count = str(item).partition(':')
        name = name.strip()
        if not name:
            raise ValueError(f'{item!r} names nothing')
        if count.strip():
            try:
                number = int(count)
            except ValueError:
                raise ValueError(f'{count!r} is not a whole number') from None
            if number < least:
                raise ValueError(f'a count of {number} creates nothing'
                                 if least else
                                 f'a count of {number} is not a number of '
                                 f'cartridges')
        else:
            number = max(least, 1)
        runs.append({what: name, 'count': number})
    return runs


def _runs_of(names):
    """[(name, count), ...], collapsing a repeat of one name into a run.

    The inverse of the expansion apply_defaults does, so drive_slots reads
    back as the list it was built from. Only *consecutive* repeats collapse:
    slot order is SCSI target order, and a library whose first and third slots
    hold one model is not the same library as one whose first two do.
    """
    runs = []
    for name in names:
        if runs and runs[-1][0] == name:
            runs[-1] = (name, runs[-1][1] + 1)
        else:
            runs.append((name, 1))
    return runs


def _whole(value):
    """A count as a whole number, or None when there is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _each_once(values) -> List[str]:
    """The values that are set, each once, in the order they appear.

    Order is slot order, so a message about the first drive names the first
    drive. A set would scramble that and make the output differ between runs.
    """
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _nominal_density(drive_slots, asked_media) -> str:
    """The density a library of these drives is said to hold.

    The rule `mhvtl tape media` reports for a library that already exists,
    applied to one that does not yet: the first density its drives can write,
    in slot order. It falls back to the first run asked for, so a
    specification whose drives write nothing still names a density and
    validation gets to refuse it with a reason.
    """
    from ..profiles import catalogue

    return (catalogue.default_density_for(slot['product'] for slot in drive_slots)
            or asked_media[0][0])


def _default_drive(key: str, product: str, profile) -> str:
    """The drive this library model gets when none is chosen."""
    try:
        return get_default_drive_for_library(key, product)
    except (KeyError, ValueError):
        return profile.drive_product_default


def _default_media(key: str, drive: str, profile) -> str:
    """The media that drive writes natively, or the profile's default."""
    try:
        return get_default_media_for_drive(key, drive)
    except (KeyError, ValueError):
        return profile.library_default_media


def drive_ids(library_id: int, num_drives: int):
    """The ids a new library's drives would get on an empty host.

    library_id + 1 .. + n - the convention MHVTL's demo configuration uses. On a
    real host config/ids.drive_ids decides, preferring these and moving past any
    that are taken or would collide with a library id.
    """
    return [int(library_id) + slot for slot in range(1, int(num_drives) + 1)]
