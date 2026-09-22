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
from typing import Any, Dict, List

from ..core import ValidationResult
from ..profiles import personalities
from ..profiles.data import (PROFILES, get_default_drive_for_library,
                             get_default_media_for_drive, get_media_suffix,
                             get_profile, get_valid_drives_for_library,
                             get_valid_media_for_drive, lto_generation,
                             lto_read_only_media)

logger = logging.getLogger(__name__)

#: MHVTL truncates a unit serial number to this many characters.
MAX_SERIAL_LENGTH = 10


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
    # MHVTL emulates, checked against its layout below.
    num_drives = int(library_data.get("num_drives", 4))
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

    # Validate library model (if specified)
    library_model = library_data.get("library_model") or library_data.get("product")
    if library_model and library_model not in profile.library_models:
        errors.append(f"Library model '{library_model}' is not valid for {profile_key}")
        fixes.append(f"Valid library models for {profile_key}: {', '.join(profile.library_models)}")

    # Validate drive model (if specified)
    drive_model = library_data.get("drive_model") or library_data.get("drive_product")
    if drive_model and drive_model not in profile.drive_models:
        errors.append(f"Drive model '{drive_model}' is not valid for {profile_key}")
        fixes.append(f"Valid drive models for {profile_key}: {', '.join(profile.drive_models)}")

    # Validate drive model is compatible with library model (cascading validation)
    if library_model and profile.library_drive_mapping:
        valid_drives = profile.library_drive_mapping.get(library_model, [])
        effective_drive = drive_model or _model_default_drive(profile_key, library_model, profile)
        if valid_drives and effective_drive not in valid_drives:
            errors.append(f"Drive model '{effective_drive}' is not compatible with library model '{library_model}'")
            fixes.append(f"Library '{library_model}' supports drives: {', '.join(valid_drives)}")

    # Validate drive vendor (if specified)
    drive_vendor = library_data.get("drive_vendor")
    if drive_vendor and drive_vendor not in profile.allowed_drive_vendors:
        errors.append(f"Drive vendor '{drive_vendor}' is not allowed for {profile_key} libraries")
        fixes.append(f"Allowed drive vendors: {', '.join(profile.allowed_drive_vendors)}")

    # Validate media type compatibility
    media_type = library_data.get("media_type")
    if media_type:
        # Check media is supported by library
        if media_type not in profile.library_supported_media:
            errors.append(f"Media type '{media_type}' is not supported by {profile_key} libraries")
            fixes.append(f"Supported media types: {', '.join(profile.library_supported_media)}")

        # Check media is supported by the selected drive model
        effective_drive_model = drive_model or _model_default_drive(profile_key, library_model, profile)
        if effective_drive_model and profile.drive_media_by_model:
            drive_supported_media = profile.drive_media_by_model.get(effective_drive_model, [])
            if media_type not in drive_supported_media:
                errors.append(f"Media type '{media_type}' is not supported by drive model '{effective_drive_model}'")
                fixes.append(f"Drive '{effective_drive_model}' supports: {', '.join(drive_supported_media)}")

        # A cartridge the drive loads but cannot write makes a library that
        # restores and never backs up. Allowed, because it is what MHVTL does,
        # but said.
        generation = lto_generation(effective_drive_model or '')
        if generation and media_type in lto_read_only_media(generation):
            warnings.append(f"Media type '{media_type}' is read-only in "
                            f"'{effective_drive_model}': backups to it will fail")

        # Validate media suffix exists
        try:
            get_media_suffix(media_type)
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
    media_count = int(library_data.get(
        "media_count", min(profile.default_media_count, layout.max_slots)))
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

    effective_drive = drive_model or _model_default_drive(profile_key, library_model, profile)
    if personalities.drive_personality(effective_drive) == personalities.GENERIC_DRIVE:
        errors.append(f"MHVTL does not recognise the drive model '{effective_drive}' "
                      f"and would emulate a generic drive (usr/cmd/vtltape.c "
                      f"tape_drives[])")

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

    return ValidationResult(is_valid=(len(errors) == 0), errors=errors, warnings=warnings, suggested_fixes=fixes)

