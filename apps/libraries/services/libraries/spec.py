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
from typing import Any, Dict

from ..core import ValidationFailed
from ..profiles import personalities
from ..profiles.data import (PROFILES, get_default_drive_for_library,
                             get_default_media_for_drive, get_media_suffix,
                             get_profile)

logger = logging.getLogger(__name__)

#: MHVTL truncates a unit serial number to this many characters.
MAX_SERIAL_LENGTH = 10


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


def truncate_serial(serial: str) -> str:
    """Serials longer than the SCSI field are cut, not rejected.

    The field is fixed width in the inquiry response; MHVTL truncates it anyway,
    and rejecting one is a worse answer than reporting what will be used.
    """
    return (serial or '').strip()[:MAX_SERIAL_LENGTH]


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
    filled['serial'] = truncate_serial(filled.get('serial') or f'XYZZY_{library_id}')

    # What MHVTL will emulate for these strings. A default must fit the
    # model's element layout: the OVERLAND profile's default of four MAP slots
    # went into a layout with room for one, and MHVTL silently dropped three.
    # An explicit value over the limit is left for validation to refuse.
    layout = personalities.library_layout(filled['vendor'], filled['product'])

    # Drive identity.
    filled.setdefault('num_drives', min(profile.default_num_drives,
                                        layout.max_drives))
    filled.setdefault('drive_vendor', profile.drive_vendor)
    # The model's own default drive, not the profile's: a profile has one
    # default, and it is not a drive every model of that vendor can carry.
    filled.setdefault('drive_product', filled.get('drive_model')
                      or _default_drive(key, filled['product'], profile))
    filled.setdefault('drive_revision', profile.drive_revision_default)

    # SCSI addressing. The target is allocated at write time against the
    # device.conf as it stands, so it is deliberately not defaulted here.
    filled.setdefault('channel', 0)
    filled.setdefault('lun', 0)

    # Media and barcodes.
    filled.setdefault('media_type', _default_media(key, filled['drive_product'],
                                                   profile))
    try:
        filled['media_suffix'] = get_media_suffix(filled['media_type'])
    except ValueError:
        # An unsupported media type is validation's to report, with the list of
        # what this profile does support. Leaving the suffix out is enough to
        # stop anything being written.
        filled.pop('media_suffix', None)

    filled.setdefault('media_count', min(profile.default_media_count,
                                         layout.max_slots))
    filled.setdefault('empty_slots', min(profile.default_empty_slots,
                                         layout.max_slots - filled['media_count']))
    filled.setdefault('map_count', min(profile.default_num_maps, layout.max_maps))
    filled.setdefault('barcode_prefix', f'{profile.barcode_leading}{library_id:02d}')

    return filled


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
