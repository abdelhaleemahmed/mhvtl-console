"""Checking a library specification before anything is written.

Moved from mhvtl_library_service.py:validate_library, the strict version that
reads the vendor profile rather than guessing: whether the model exists for that
vendor, whether the drive model may be fitted to that library, and whether the
media is supported by both the library and the drive. It catches the
combinations MHVTL would accept into device.conf and then fail on - an LTO8
cartridge in a T10000 drive, say.

The rules themselves live in services/profiles; this applies them.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Any, Dict, List, Tuple

from ..core import ValidationResult
from ..profiles import personalities
from .spec import (asked_densities, asked_drive_count, asked_drive_models,
                   asked_media_count, profile_key_of)
from ..profiles.data import (PROFILES, get_default_drive_for_library,
                             get_default_media_for_drive, get_media_suffix,
                             get_profile, get_valid_drives_for_library,
                             get_valid_media_for_drive, lto_generation,
                             lto_read_only_media)

logger = logging.getLogger(__name__)

#: MHVTL truncates a unit serial number to this many characters.
MAX_SERIAL_LENGTH = 10

#: What to assume when nothing says how many drives. Only a fallback: a
#: specification on its way to creation has been through spec.apply_defaults,
#: which takes the number from the profile and caps it at the model's layout.
#: It is reached by a half-built preset, where the question may still be
#: unanswered and the layout checks need a number to work with.
DEFAULT_NUM_DRIVES = 4


def _truncate_serial(serial: str) -> str:
    """Serial numbers longer than the SCSI field are cut, not rejected."""
    serial = (serial or '').strip()
    return serial[:MAX_SERIAL_LENGTH]


def _model_default_drive(profile_key, library_model, profile) -> str:
    """The drive spec.apply_defaults would choose for this model."""
    if not library_model:
        return profile.drive_product_default
    try:
        return get_default_drive_for_library(profile_key, library_model)
    except (KeyError, ValueError):
        return profile.drive_product_default


def validate(library_data: Dict[str, Any], config_dir=None) -> ValidationResult:
    """
    Validate library configuration using strict profile rules.

    Validates:

    - library_id is valid
    - Profile exists
    - Library model is valid for the profile
    - Drive model is valid for the profile
    - Media type is supported by BOTH library AND drive
    - Drive vendor is allowed for this library
    - The drive, slot and MAP counts fit the element layout of the model MHVTL
      will emulate (profiles/personalities)
    - MHVTL recognises the drive model rather than emulating a generic drive
    - The library id is free across libraries and drives, and there are
      enough drive ids and SCSI targets left for the library
    """
    errors: List[str] = []
    warnings: List[str] = []
    fixes: List[str] = []

    if "library_id" not in library_data:
        errors.append("library_id is required")
        return ValidationResult(False, errors, warnings, fixes)

    try:
        library_id = int(library_data["library_id"])
    except Exception:
        errors.append("library_id must be an integer")
        return ValidationResult(False, errors, warnings, fixes)

    if not 1 <= library_id <= personalities.MAX_DEVICE_ID:
        errors.append(f"library_id must be between 1 and "
                      f"{personalities.MAX_DEVICE_ID}; MHVTL uses it as a kernel "
                      f"minor number and refuses anything larger")
    elif library_id > personalities.MAX_NAA_FIELD:
        warnings.append(f"library_id {library_id} has three digits, so MHVTL will "
                        f"ignore the configured NAA and derive one from the "
                        f"serial number")

    # No fixed cap: how many drives a library can hold depends on the model
    # MHVTL emulates, checked against its layout below. A mixed library says
    # how many by listing them, so the count is asked for rather than read.
    num_drives = asked_drive_count(library_data, DEFAULT_NUM_DRIVES)
    if num_drives < 1:
        errors.append("num_drives must be at least 1")

    # Validate profile exists
    profile_key = library_data.get("profile") or library_data.get("vendor_profile") or library_data.get("vendor_key")
    if not profile_key:
        errors.append("Profile/vendor key is required")
        return ValidationResult(False, errors, warnings, fixes)

    try:
        profile = get_profile(profile_key)
    except KeyError as e:
        errors.append(f"Invalid profile: {e}")
        fixes.append(f"Valid profiles are: {', '.join(sorted(PROFILES.keys()))}")
        return ValidationResult(False, errors, warnings, fixes)

    profile_errors, profile_warnings, profile_fixes = check_against_profile(
        library_data, profile_key, profile)
    errors.extend(profile_errors)
    warnings.extend(profile_warnings)
    fixes.extend(profile_fixes)

    # The id namespace and the SCSI targets on this host. A device.conf that
    # cannot be read is not an error here: the first library on a host is
    # created before the file exists.
    from ..config import device_conf as device_conf_format
    from ..config import ids
    from ..core import device_conf_path, shell

    existing = shell.sudo_cat(device_conf_path(config_dir))
    conf = device_conf_format.parse(existing.stdout if existing.ok else '')
    if library_id in conf.libraries:
        errors.append(f"Library {library_id} already exists in device.conf")
    elif library_id in conf.drives:
        errors.append(f"Id {library_id} is already used by drive {library_id}; "
                      f"libraries and drives share one id namespace")
    elif not errors:
        try:
            ids.plan_library(conf, library_id, num_drives)
        except ids.OutOfIds as exc:
            errors.append(str(exc))

    return ValidationResult(is_valid=(len(errors) == 0), errors=errors,
                            warnings=warnings, suggested_fixes=fixes)


def check_against_profile(library_data: Dict[str, Any], profile_key: str,
                          profile) -> Tuple[List[str], List[str], List[str]]:
    """The vendor-profile rules alone: (errors, warnings, fixes).

    Extracted from validate() so a *partial* specification can be checked by
    the same rules. A preset under construction has no library id and no
    business reading device.conf, and validate() does both - it returns
    "library_id is required" before reaching any of this, and then asks the
    host whether the id is free. Those two concerns are the caller's; these
    rules are the profile's, and there must be one copy of them.

    The profile is passed in already resolved rather than looked up here, so
    validate() keeps its early return for an unknown profile and the lookup
    happens once.
    """
    errors: List[str] = []
    warnings: List[str] = []
    fixes: List[str] = []
    num_drives = asked_drive_count(library_data, DEFAULT_NUM_DRIVES)

    # Validate library model (if specified)
    library_model = library_data.get("library_model") or library_data.get("product")
    if library_model and library_model not in profile.library_models:
        errors.append(f"Library model '{library_model}' is not valid for {profile_key}")
        fixes.append(f"Valid library models for {profile_key}: {', '.join(profile.library_models)}")

    # Every drive model the library will hold, in slot order and each once -
    # one entry for a uniform library, several for a mixed one, and every
    # check below runs over all of them. spec.asked_drive_models reads whichever
    # shape arrived, because this is asked of a filled specification and of a
    # half-built preset alike.
    #
    # These checks used to read `drive_model` alone, which for a mixed library
    # is only the first slot's and for a mixed preset is not set at all: a TD6
    # the library model cannot take passed validation and reached device.conf.
    drive_models = asked_drive_models(library_data)

    for model in drive_models:
        if model not in profile.drive_models:
            errors.append(f"Drive model '{model}' is not valid for {profile_key}")
            fixes.append(f"Valid drive models for {profile_key}: {', '.join(profile.drive_models)}")

    # Validate drive models are compatible with the library model (cascading)
    if library_model and profile.library_drive_mapping:
        valid_drives = profile.library_drive_mapping.get(library_model, [])
        checking = drive_models or [_model_default_drive(profile_key,
                                                         library_model, profile)]
        for model in checking:
            if valid_drives and model not in valid_drives:
                errors.append(f"Drive model '{model}' is not compatible with library model '{library_model}'")
                fixes.append(f"Library '{library_model}' supports drives: {', '.join(valid_drives)}")

    # Validate drive vendor (if specified)
    drive_vendor = library_data.get("drive_vendor")
    if drive_vendor and drive_vendor not in profile.allowed_drive_vendors:
        errors.append(f"Drive vendor '{drive_vendor}' is not allowed for {profile_key} libraries")
        fixes.append(f"Allowed drive vendors: {', '.join(profile.allowed_drive_vendors)}")

    # Every density the library will hold, each once. One for a uniform
    # library, several for a mixed one, and the same rules over all of them.
    densities = asked_densities(library_data)

    effective_drive_model = (drive_models[0] if drive_models
                             else _model_default_drive(profile_key,
                                                       library_model, profile))
    for density in densities:
        # Check media is supported by library
        if density not in profile.library_supported_media:
            errors.append(f"Media type '{density}' is not supported by {profile_key} libraries")
            fixes.append(f"Supported media types: {', '.join(profile.library_supported_media)}")

        # Check the density is one some drive in the library takes. For a
        # uniform library that is the one drive model; for a mixed one, any of
        # them - an LTO-6 cartridge belongs in a library that has a TD6 even
        # though its TD8s cannot read it.
        takes = [model for model in (drive_models or [effective_drive_model])
                 if density in (profile.drive_media_by_model.get(model) or [])]
        if profile.drive_media_by_model and not takes:
            named = drive_models or [effective_drive_model]
            errors.append(f"Media type '{density}' is not supported by drive "
                          f"model '{named[0]}'" if len(named) == 1 else
                          f"Media type '{density}' is not supported by any "
                          f"drive in this library: {', '.join(named)}")
            # Only when there is something to list. The media list is empty
            # for a drive this profile does not have at all - T10000C under
            # IBM - and "Drive 'T10000C' supports:" followed by nothing
            # reads as a bug in the tool rather than a mistake in the
            # command. The error above already says the drive is wrong.
            for model in named:
                supported = profile.drive_media_by_model.get(model) or []
                if supported:
                    fixes.append(f"Drive '{model}' supports: "
                                 f"{', '.join(supported)}")

        # A cartridge a drive loads but cannot write makes a library that
        # restores and never backs up. Allowed, because it is what MHVTL does,
        # but said - and only when NO drive in the library can write it. The
        # check read the nominal drive alone, so a mixed library was warned
        # that its LTO-6 was read-only while holding the TD6 that writes it.
        writers = [model for model in takes
                   if density not in lto_read_only_media(lto_generation(model))]
        if takes and not writers:
            warnings.append(f"Media type '{density}' is read-only in "
                            f"'{takes[0]}': backups to it will fail"
                            if len(takes) == 1 else
                            f"Media type '{density}' is read-only in every "
                            f"drive this library has: backups to it will fail")

        # Validate media suffix exists
        try:
            get_media_suffix(density)
        except ValueError as e:
            errors.append(f"Invalid media type for barcode: {e}")

    # What MHVTL will emulate for these strings, and whether the counts fit
    # the element-address layout of that model. MHVTL does not report an
    # overflow as an error - it logs it and leaves the element out - so this is
    # the only place it can be caught.
    vendor = library_data.get("vendor") or profile.library_vendor
    product = library_model or profile.library_product_default
    layout = personalities.library_layout(vendor, product)
    # Unset counts take the same layout-capped defaults spec.apply_defaults
    # gives them, so a raw specification and a filled one validate alike.
    media_count = asked_media_count(
        library_data, min(profile.default_media_count, layout.max_slots))
    slots = media_count + int(library_data.get(
        "empty_slots", min(profile.default_empty_slots,
                           max(layout.max_slots - media_count, 0))))
    maps = int(library_data.get("map_count",
                                min(profile.default_num_maps, layout.max_maps)))
    for problem in personalities.limits_problems(vendor, product, drives=num_drives,
                                                  slots=slots, maps=maps):
        errors.append(problem)
        fixes.append(f"{layout.title}: up to {layout.max_drives} drives, "
                     f"{layout.max_slots} slots, {layout.max_maps} MAP slots")

    # Every model, not just the nominal one: a mixed library with one drive
    # MHVTL does not recognise would emulate a generic drive in that slot.
    for model in (drive_models or [_model_default_drive(profile_key,
                                                        library_model, profile)]):
        if personalities.drive_personality(model) == personalities.GENERIC_DRIVE:
            errors.append(f"MHVTL does not recognise the drive model '{model}' "
                          f"and would emulate a generic drive (usr/cmd/vtltape.c "
                          f"tape_drives[])")

    # The counts a list implies must match the counts given beside it. Both
    # are accepted - `--drives 4` is the shorthand - but a specification that
    # says four drives and then lists two is two answers to one question, and
    # guessing which was meant is how a library comes out the wrong size.
    #
    # The file format and the command line refuse the pair outright, so this
    # is the net under them: a preset edited in two steps - `--add-drive` and
    # then `--drives` - reaches here without passing either.
    #
    # The counts are asked of the lists alone, which is what dropping the two
    # count keys does. Both shapes are covered that way: drive_slots on a
    # filled specification, `drive` on a preset that has not been filled in.
    lists_only = {key: value for key, value in library_data.items()
                  if key not in ('num_drives', 'media_count')}
    for count_key, option, listed, what, how in (
            ('num_drives', '--drives',
             asked_drive_count(lists_only, 0), 'drives',
             'make the list add up to it'),
            ('media_count', '--tapes',
             asked_media_count(lists_only, 0), 'cartridges',
             'make the densities add up to it')):
        given = _as_count(library_data.get(count_key))
        if listed and given is not None and listed != given:
            errors.append(f'{given} {what} were asked for but the list adds '
                          f'up to {listed}')
            fixes.append(f'drop {option}, or {how}')

    return errors, warnings, fixes


def _as_count(value):
    """A count as a whole number, or None when it is not one.

    None means "do not check": a count that is not a number is already an
    error of its own, and a second message about it disagreeing with a list
    would be the same mistake reported twice.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_partial(library_data: Dict[str, Any]) -> ValidationResult:
    """Check a specification that is still being built.

    For a preset under construction, which may not have a profile yet - the
    operator is allowed to say `--drives 2` before deciding whose library it
    is. With no profile there is nothing to check against, and that is a valid
    state rather than an error; `preset list` marks such a preset incomplete.

    Never reads device.conf and never needs a library id: an id is chosen when
    the preset is *used*, and validate() checks it then.
    """
    errors: List[str] = []
    warnings: List[str] = []
    fixes: List[str] = []

    num_drives = library_data.get("num_drives")
    if num_drives is not None:
        try:
            if int(num_drives) < 1:
                errors.append("num_drives must be at least 1")
        except (TypeError, ValueError):
            errors.append(f"num_drives must be a whole number, "
                          f"not {num_drives!r}")

    profile_key = profile_key_of(library_data)
    if not profile_key:
        # Legal: a preset may be built up a piece at a time, and the profile
        # may be the last piece. Nothing below can be checked without it.
        return ValidationResult(is_valid=not errors, errors=errors,
                                warnings=warnings, suggested_fixes=fixes)

    try:
        profile = get_profile(profile_key)
    except KeyError as exc:
        errors.append(f"Invalid profile: {exc}")
        fixes.append(f"Valid profiles are: {', '.join(sorted(PROFILES.keys()))}")
        return ValidationResult(is_valid=False, errors=errors, warnings=warnings,
                                suggested_fixes=fixes)

    profile_errors, profile_warnings, profile_fixes = check_against_profile(
        library_data, profile_key, profile)
    errors.extend(profile_errors)
    warnings.extend(profile_warnings)
    fixes.extend(profile_fixes)
    return ValidationResult(is_valid=not errors, errors=errors,
                            warnings=warnings, suggested_fixes=fixes)

