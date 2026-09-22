"""Vendor and model reference data.

Moved from services/mhvtl_profiles.py, unchanged: which libraries a vendor
makes, which drives each library may carry, which media each drive accepts, and
how a serial number is formed. Pure data and lookups - no I/O, no Django - which
is why it needed no adaptation.

These rules are what services/libraries still delegates create_library for.
They are the reason that delegation exists: re-deriving a table of vendor quirks
is how the two earlier forks of that service started.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""


from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Dict, List, Optional

from .personalities import (SUFFIX_BY_DENSITY, _lto, creatable_media,
                            drive_personality, library_layout, read_only_media,
                            scalar_layout_product)


# ------------------------------------------------------------
# Media suffix mapping (barcode suffixes)
# ------------------------------------------------------------

#: Media type -> barcode suffix. The table lives in personalities, beside the
#: MHVTL source it comes from; this name is kept for the views that import it.
MEDIA_SUFFIX: Dict[str, str] = SUFFIX_BY_DENSITY


def get_media_suffix(media_type: str) -> str:
    """Return the barcode suffix for a given media type.

    STRICT: unknown media types raise ValueError.
    """
    if not media_type:
        raise ValueError("media_type is empty")
    key = media_type.strip().upper()
    if key not in MEDIA_SUFFIX:
        raise ValueError(f"Unknown/unsupported media type for barcode suffix: {media_type!r}")
    return MEDIA_SUFFIX[key]


# ------------------------------------------------------------
# Strict vendor profile definition
# ------------------------------------------------------------

@dataclass(frozen=True)
class VendorProfile:
    key: str

    # Library inquiry identity
    library_vendor: str
    library_models: List[str]
    library_product_default: str
    library_revision_default: str

    # Library media types supported by this personality (mt values)
    library_supported_media: List[str]
    library_default_media: str

    # Drive inquiry identity
    # NOTE: Some vendors legitimately use different drive vendors (e.g., ADIC+IBM).
    drive_vendor: str
    drive_models: List[str]
    drive_product_default: str
    drive_revision_default: str
    drive_vpd_hex: str = "b0 04 00 02 01 00"

    # Strict validation: per-drive-model supported media
    drive_media_by_model: Dict[str, List[str]] = None  # type: ignore[assignment]

    # Allowed drive vendors for this library (strict mixing policy)
    allowed_drive_vendors: List[str] = None  # type: ignore[assignment]

    # Library model to allowed drives mapping (cascading validation)
    # Each library model maps to a list of drive models that are valid for it
    # Based on MHVTL source "should contain" logic (ibm_smc_pm.c:442-471)
    library_drive_mapping: Dict[str, List[str]] = None  # type: ignore[assignment]

    # Legacy GUI defaults (safe, not identity-critical)
    default_num_drives: int = 2
    default_num_maps: int = 4
    default_media_count: int = 50
    # Four spare slots, so a library is not born full: a tape can be added
    # without reconfiguring it. An empty slot costs one line in
    # library_contents and one element in the robot's inventory, no disk.
    default_empty_slots: int = 4

    # Barcode behavior
    barcode_leading: str = "L"

    @property
    def default_media_suffix(self) -> str:
        return get_media_suffix(self.library_default_media)

    def assert_strict(self) -> None:
        """Fail-fast sanity checks to keep registry consistent."""
        if self.library_vendor != self.key:
            raise ValueError(f"[{self.key}] library_vendor must equal key: {self.library_vendor} != {self.key}")

        if self.library_product_default not in self.library_models:
            raise ValueError(f"[{self.key}] library_product_default must be in library_models")

        if self.library_default_media not in self.library_supported_media:
            raise ValueError(f"[{self.key}] library_default_media must be in library_supported_media")

        if self.drive_product_default not in self.drive_models:
            raise ValueError(f"[{self.key}] drive_product_default must be in drive_models")

        if not self.drive_media_by_model:
            raise ValueError(f"[{self.key}] drive_media_by_model must be set")

        if self.drive_product_default not in self.drive_media_by_model:
            raise ValueError(f"[{self.key}] drive_product_default missing from drive_media_by_model")

        if not self.allowed_drive_vendors:
            raise ValueError(f"[{self.key}] allowed_drive_vendors must be set")

        if self.drive_vendor not in self.allowed_drive_vendors:
            raise ValueError(f"[{self.key}] drive_vendor must be included in allowed_drive_vendors")

        # Default media must be supported by default drive model
        supported = self.drive_media_by_model.get(self.drive_product_default, [])
        if self.library_default_media not in supported:
            raise ValueError(
                f"[{self.key}] default media {self.library_default_media} not supported by default drive {self.drive_product_default}"
            )

        # Validate library_drive_mapping if provided
        if self.library_drive_mapping:
            # All library models in mapping must be in library_models
            for lib_model in self.library_drive_mapping.keys():
                if lib_model not in self.library_models:
                    raise ValueError(
                        f"[{self.key}] library_drive_mapping has unknown library model: {lib_model}"
                    )
            # All drives in mapping must be in drive_models
            for lib_model, drives in self.library_drive_mapping.items():
                for drive in drives:
                    if drive not in self.drive_models:
                        raise ValueError(
                            f"[{self.key}] library_drive_mapping[{lib_model}] has unknown drive: {drive}"
                        )
            # Default library model must have default drive in its mapping
            if self.library_product_default in self.library_drive_mapping:
                allowed_drives = self.library_drive_mapping[self.library_product_default]
                if self.drive_product_default not in allowed_drives:
                    raise ValueError(
                        f"[{self.key}] default drive {self.drive_product_default} not in mapping for default library {self.library_product_default}"
                    )


# ------------------------------------------------------------
# Helper: LTO backward compatibility
# Based on MHVTL 1.7 source (ult3580_pm.c)
# ------------------------------------------------------------

def _lto_media(gen: int) -> List[str]:
    """The cartridges an LTO-`gen` drive loads, native first, as MHVTL 1.8 has it.

    Read from personalities._lto(), which carries the table and its source
    lines:

        LTO-1         LTO1
        LTO-2         LTO2, LTO1
        LTO-3 .. 7    LTOn, LTOn-1 read/write; LTOn-2 read-only
        LTO-8         LTO8, LTO7
        LTO-9         LTO9, LTO8
        LTO-10        LTO10, LTO10P

    This replaced _lto_chain(), whose docstring said "read 2 generations back"
    and which returned every generation down to LTO-1 - so an LTO-7 library
    could be created with LTO-3 tapes that MHVTL refuses to load.
    tests/test_personalities checks this against the MHVTL source.
    """
    return list(_lto(gen).loads)


def lto_read_only_media(gen: int) -> List[str]:
    """The cartridges an LTO-`gen` drive loads but cannot write.

    A library built on them restores and never backs up, which is worth a
    warning rather than a refusal. personalities.read_only_media() answers the
    same question for any drive, by product string.
    """
    return list(_lto(gen).read_only) if 1 <= gen <= 10 else []


def _media(product: str) -> List[str]:
    """The media a non-LTO drive loads, from personalities.DRIVE_MEDIA.

    These lists used to be written by hand here, and named drives where MHVTL
    wants densities (T10000A for T10KA, DLT7000 for DLT4) - mktape refuses
    those names - and gave the 9840C/D and SDLT600 cartridges they refuse.
    """
    return creatable_media(product)


#: Quantum-branded LTO drives, which report ULTRIUM-TDn (full height) or
#: ULTRIUM-HHn (half height). vtltape maps both to the IBM LTO personalities
#: (usr/cmd/vtltape.c:150-168); offered on Quantum's tape library models.
#: IBM-branded LTO drives, full height and half height, as vtltape recognises
#: them (usr/cmd/vtltape.c tape_drives[]). Libraries whose maker does not build
#: drives - ADIC, Dell, Overland, Spectra - ship these.
IBM_LTO_DRIVES = [f'ULT3580-TD{g}' for g in '123456789'] + ['ULT3580-TDA'] + \
    [f'ULT3580-HH{g}' for g in '789'] + ['ULT3580-HHA']

QUANTUM_LTO_DRIVES = ["ULTRIUM-TD1", "ULTRIUM-TD2", "ULTRIUM-TD3", "ULTRIUM-TD4", "ULTRIUM-TD5", "ULTRIUM-TD6", "ULTRIUM-TD7", "ULTRIUM-TD8", "ULTRIUM-TD9", "ULTRIUM-TDA", "ULTRIUM-HH2", "ULTRIUM-HH3", "ULTRIUM-HH4", "ULTRIUM-HH5", "ULTRIUM-HH6", "ULTRIUM-HH7", "ULTRIUM-HH8", "ULTRIUM-HH9", "ULTRIUM-HHA"]


# ------------------------------------------------------------
# Profile registry (Updated for MHVTL 1.7)
# Source: vtltape.c, vtllibrary.c, *_pm.c files
# ------------------------------------------------------------

PROFILES: Dict[str, VendorProfile] = {
    # ===== IBM =====
    # Libraries: 3573-TL (TS3100), 03584* (TS3500)
    # Drives: ULT3580-TD1 through TD9, 03592* (3592 enterprise)
    # Based on MHVTL 1.7 source and IBM "should contain" logic
    "IBM": VendorProfile(
        key="IBM",
        library_vendor="IBM",
        library_models=[
            # TS3500 LTO libraries (L=LTO, D=dual-drive frame variant)
            "03584L32",   # TS3500 with LTO drives (most common)
            "03584D32",   # TS3500 dual-drive frame with LTO
            "03584L52",   # TS3500 LTO variant
            "03584D52",   # TS3500 dual-drive LTO variant
            "03584L53",   # TS3500 LTO variant
            "03584D53",   # TS3500 dual-drive LTO variant
            # TS3500 3592 libraries
            "03584L22",   # TS3500 with 3592 drives
            "03584D22",   # TS3500 dual-drive frame with 3592
            "03584L23",   # TS3500 3592 variant
            "03584D23",   # TS3500 dual-drive 3592 variant
            # TS3500 DLT libraries
            "03584L42",   # TS3500 with DLT drives
            # TS3100 series (LTO only)
            "3573-TL",    # TS3100 series
            "ULT3582-TL", # Legacy Ultrium library
        ],
        library_product_default="03584L32",
        library_revision_default="D.02",
        library_supported_media=[
            # LTO formats (all generations)
            "LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8", "LTO9", "LTO10", "LTO10P",
            # IBM 3592 enterprise formats
            "J1A", "E05", "E06", "E07",
            # DLT IV, the only cartridge a DLT7000/8000 writes (for 03584L42)
            "DLT4",
        ],
        library_default_media="LTO8",
        drive_vendor="IBM",
        drive_models=[
            # LTO full-height drives (TD = Tape Drive)
            "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
            "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            # LTO half-height drives (HH = Half Height)
            "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            # IBM 3592 enterprise drives
            "03592J1A",   # 3592 Gen 1
            "03592E05",   # 3592 Gen 2 (TS1120)
            "03592E06",   # 3592 Gen 3 (TS1130)
            "03592E07",   # 3592 Gen 4 (TS1140)
            # DLT drives (for legacy 03584L42 library)
            "DLT7000", "DLT8000",
        ],
        drive_product_default="ULT3580-TD8",
        drive_revision_default="D.02",
        drive_media_by_model={
            # LTO full-height drives
            "ULT3580-TD1": _lto_media(1),
            "ULT3580-TD2": _lto_media(2),
            "ULT3580-TD3": _lto_media(3),
            "ULT3580-TD4": _lto_media(4),
            "ULT3580-TD5": _lto_media(5),
            "ULT3580-TD6": _lto_media(6),
            "ULT3580-TD7": _lto_media(7),
            "ULT3580-TD8": _lto_media(8),  # LTO8, LTO7 ONLY
            "ULT3580-TD9": _lto_media(9),  # LTO9, LTO8 ONLY
            "ULT3580-TDA": _lto_media(10),  # LTO10, LTO10P ONLY
            # LTO half-height drives
            "ULT3580-HH7": _lto_media(7),
            "ULT3580-HH8": _lto_media(8),
            "ULT3580-HH9": _lto_media(9),
            "ULT3580-HHA": _lto_media(10),
            # IBM 3592 enterprise drives
            "03592J1A": _media("03592J1A"),
            "03592E05": _media("03592E05"),
            "03592E06": _media("03592E06"),
            "03592E07": _media("03592E07"),
            # DLT drives
            "DLT7000": _media("DLT7000"),
            "DLT8000": _media("DLT8000"),
        },
        allowed_drive_vendors=["IBM"],
        # Cascading validation: which drives are valid for which library model
        # Based on IBM "should contain" logic from MHVTL source (ibm_smc_pm.c:442-471)
        library_drive_mapping={
            # LTO libraries → LTO drives only
            "03584L32": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            "03584D32": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            "03584L52": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            "03584D52": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            "03584L53": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            "03584D53": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            # 3592 libraries → 3592 drives only
            "03584L22": ["03592J1A", "03592E05", "03592E06", "03592E07"],
            "03584D22": ["03592J1A", "03592E05", "03592E06", "03592E07"],
            "03584L23": ["03592J1A", "03592E05", "03592E06", "03592E07"],
            "03584D23": ["03592J1A", "03592E05", "03592E06", "03592E07"],
            # DLT library → DLT drives only
            "03584L42": ["DLT7000", "DLT8000"],
            # TS3100 series → LTO drives only
            "3573-TL": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
            # Legacy Ultrium library → LTO drives only
            "ULT3582-TL": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "ULT3580-HH7", "ULT3580-HH8", "ULT3580-HH9", "ULT3580-HHA",
            ],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="I",
    ),

    # ===== STK (StorageTek / Oracle) =====
    # Libraries: SL500, SL150, SL3000, L700, L180, L20/40/80
    # Drives: T10000*, T9840*, T9940*, LTO drives
    # Enterprise libraries support multiple drive families
    "STK": VendorProfile(
        key="STK",
        library_vendor="STK",
        library_models=["SL500", "SL150", "SL3000", "L700", "L180", "L120", "L20", "L40", "L80"],
        library_product_default="SL500",
        library_revision_default="0016",
        library_supported_media=[
            "T10KA", "T10KB", "T10KC",
            "9840A", "9840B", "9840C", "9840D",
            "9940A", "9940B",
            "LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8", "LTO9", "LTO10", "LTO10P",
            "DLT4",
        ],
        library_default_media="T10KC",
        drive_vendor="STK",
        drive_models=[
            "T10000A", "T10000B", "T10000C",
            "T9840A", "T9840B", "T9840C", "T9840D",
            "T9940A", "T9940B",
            "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
            "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            "DLT7000",
        ],
        drive_product_default="T10000C",
        drive_revision_default="0016",
        drive_media_by_model={
            "T10000A": _media("T10000A"),
            "T10000B": _media("T10000B"),
            "T10000C": _media("T10000C"),
            "T9840A": _media("T9840A"),
            "T9840B": _media("T9840B"),
            "T9840C": _media("T9840C"),
            "T9840D": _media("T9840D"),
            "T9940A": _media("T9940A"),
            "T9940B": _media("T9940B"),
            "ULT3580-TD3": _lto_media(3),
            "ULT3580-TD4": _lto_media(4),
            "ULT3580-TD5": _lto_media(5),
            "ULT3580-TD6": _lto_media(6),
            "ULT3580-TD7": _lto_media(7),
            "ULT3580-TD8": _lto_media(8),
            "ULT3580-TD9": _lto_media(9),
            "ULT3580-TDA": _lto_media(10),  # LTO10, LTO10P ONLY
            "DLT7000": _media("DLT7000"),
        },
        allowed_drive_vendors=["STK", "IBM"],
        # Cascading validation: which drives are valid for which library model
        # STK enterprise libraries support multiple drive families
        library_drive_mapping={
            # SL3000/SL500 - enterprise libraries supporting all drive types
            "SL3000": [
                "T10000A", "T10000B", "T10000C",
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            "SL500": [
                "T10000A", "T10000B", "T10000C",
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            # SL150 - modern LTO-focused library
            "SL150": [
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            # L700/L180/L120 - legacy enterprise, 9840/9940 + LTO
            "L700": [
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "DLT7000",
            ],
            "L180": [
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "DLT7000",
            ],
            "L120": [
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
                "DLT7000",
            ],
            # L20/L40/L80 - smaller libraries, 9840/9940 + LTO
            "L20": [
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            "L40": [
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            "L80": [
                "T9840A", "T9840B", "T9840C", "T9840D",
                "T9940A", "T9940B",
                "ULT3580-TD3", "ULT3580-TD4", "ULT3580-TD5", "ULT3580-TD6",
                "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="K",
    ),

    # ===== HP =====
    # Libraries: MSL*, EML*
    # Drives: Ultrium 1-SCSI through Ultrium 8-SCSI
    # All HP libraries use HP Ultrium LTO drives
    "HP": VendorProfile(
        key="HP",
        library_vendor="HP",
        library_models=[
            "MSL G3 Series",
            "MSL6000 Series",
            "EML E-Series",
            "ESL E-Series",
        ],
        library_product_default="MSL G3 Series",
        library_revision_default="1068",
        library_supported_media=["LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8"],
        library_default_media="LTO8",
        drive_vendor="HP",
        drive_models=[
            "Ultrium 1-SCSI", "Ultrium 2-SCSI", "Ultrium 3-SCSI", "Ultrium 4-SCSI",
            "Ultrium 5-SCSI", "Ultrium 6-SCSI", "Ultrium 7-SCSI", "Ultrium 8-SCSI",
        ],
        drive_product_default="Ultrium 8-SCSI",
        drive_revision_default="1068",
        drive_media_by_model={
            "Ultrium 1-SCSI": _lto_media(1),
            "Ultrium 2-SCSI": _lto_media(2),
            "Ultrium 3-SCSI": _lto_media(3),
            "Ultrium 4-SCSI": _lto_media(4),
            "Ultrium 5-SCSI": _lto_media(5),
            "Ultrium 6-SCSI": _lto_media(6),
            "Ultrium 7-SCSI": _lto_media(7),
            "Ultrium 8-SCSI": _lto_media(8),  # LTO8, LTO7 ONLY
        },
        allowed_drive_vendors=["HP"],
        # All HP libraries use HP Ultrium drives
        library_drive_mapping={
            "MSL G3 Series": [
                "Ultrium 1-SCSI", "Ultrium 2-SCSI", "Ultrium 3-SCSI", "Ultrium 4-SCSI",
                "Ultrium 5-SCSI", "Ultrium 6-SCSI", "Ultrium 7-SCSI", "Ultrium 8-SCSI",
            ],
            "MSL6000 Series": [
                "Ultrium 1-SCSI", "Ultrium 2-SCSI", "Ultrium 3-SCSI", "Ultrium 4-SCSI",
                "Ultrium 5-SCSI", "Ultrium 6-SCSI", "Ultrium 7-SCSI", "Ultrium 8-SCSI",
            ],
            "EML E-Series": [
                "Ultrium 1-SCSI", "Ultrium 2-SCSI", "Ultrium 3-SCSI", "Ultrium 4-SCSI",
                "Ultrium 5-SCSI", "Ultrium 6-SCSI", "Ultrium 7-SCSI", "Ultrium 8-SCSI",
            ],
            "ESL E-Series": [
                "Ultrium 1-SCSI", "Ultrium 2-SCSI", "Ultrium 3-SCSI", "Ultrium 4-SCSI",
                "Ultrium 5-SCSI", "Ultrium 6-SCSI", "Ultrium 7-SCSI", "Ultrium 8-SCSI",
            ],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="H",
    ),

    # ===== QUANTUM =====
    # Libraries: Scalar i500, DXi6700, PX720
    # Drives: SDLT600, DLT7000, DLT8000
    "QUANTUM": VendorProfile(
        key="QUANTUM",
        library_vendor="QUANTUM",
        library_models=["Scalar i500", "Scalar i6000", "DXi6700", "PX720", "DX5000"],
        library_product_default="Scalar i500",
        library_revision_default="0029",
        library_supported_media=["SDLT600", "SDLT320", "SDLT220", "DLT4",
                                 "LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8", "LTO9", "LTO10", "LTO10P"],
        library_default_media="SDLT600",
        drive_vendor="QUANTUM",
        drive_models=["SDLT600", "SDLT 320", "DLT7000", "DLT8000",
                      # Quantum-branded LTO drives (vtltape.c:150-168)
                      "ULTRIUM-TD1", "ULTRIUM-TD2", "ULTRIUM-TD3", "ULTRIUM-TD4", "ULTRIUM-TD5", "ULTRIUM-TD6", "ULTRIUM-TD7", "ULTRIUM-TD8", "ULTRIUM-TD9", "ULTRIUM-TDA", "ULTRIUM-HH2", "ULTRIUM-HH3", "ULTRIUM-HH4", "ULTRIUM-HH5", "ULTRIUM-HH6", "ULTRIUM-HH7", "ULTRIUM-HH8", "ULTRIUM-HH9", "ULTRIUM-HHA"],
        drive_product_default="SDLT600",
        drive_revision_default="0029",
        drive_media_by_model={
            "SDLT600": _media("SDLT600"),
            "SDLT 320": _media("SDLT 320"),
            "DLT7000": _media("DLT7000"),
            "DLT8000": _media("DLT8000"),
            "ULTRIUM-TD1": _lto_media(1),
            "ULTRIUM-TD2": _lto_media(2),
            "ULTRIUM-TD3": _lto_media(3),
            "ULTRIUM-TD4": _lto_media(4),
            "ULTRIUM-TD5": _lto_media(5),
            "ULTRIUM-TD6": _lto_media(6),
            "ULTRIUM-TD7": _lto_media(7),
            "ULTRIUM-TD8": _lto_media(8),
            "ULTRIUM-TD9": _lto_media(9),
            "ULTRIUM-TDA": _lto_media(10),
            "ULTRIUM-HH2": _lto_media(2),
            "ULTRIUM-HH3": _lto_media(3),
            "ULTRIUM-HH4": _lto_media(4),
            "ULTRIUM-HH5": _lto_media(5),
            "ULTRIUM-HH6": _lto_media(6),
            "ULTRIUM-HH7": _lto_media(7),
            "ULTRIUM-HH8": _lto_media(8),
            "ULTRIUM-HH9": _lto_media(9),
            "ULTRIUM-HHA": _lto_media(10),
        },
        allowed_drive_vendors=["QUANTUM"],
        # All QUANTUM libraries support all QUANTUM drives
        library_drive_mapping={
            "Scalar i500": ["SDLT600", "SDLT 320", "DLT7000", "DLT8000", *QUANTUM_LTO_DRIVES],
            "Scalar i6000": ["SDLT600", "SDLT 320", "DLT7000", "DLT8000", *QUANTUM_LTO_DRIVES],
            "DXi6700": ["SDLT600", "SDLT 320", "DLT7000", "DLT8000"],
            "PX720": ["SDLT600", "SDLT 320", "DLT7000", "DLT8000", *QUANTUM_LTO_DRIVES],
            "DX5000": ["SDLT600", "SDLT 320", "DLT7000", "DLT8000"],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="Q",
    ),

    # ===== ADIC (now Quantum) =====
    # Libraries: Scalar i2000, Scalar 1000
    # Drives: Uses IBM ULT3580 drives
    "ADIC": VendorProfile(
        key="ADIC",
        library_vendor="ADIC",
        library_models=["Scalar i2000", "Scalar 1000", "scalar"],
        library_product_default="Scalar i2000",
        library_revision_default="500A",
        library_supported_media=["LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8", "LTO9", "LTO10", "LTO10P"],
        library_default_media="LTO8",
        drive_vendor="IBM",
        drive_models=[
            "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
            "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
        ],
        drive_product_default="ULT3580-TD8",
        drive_revision_default="D.02",
        drive_media_by_model={
            "ULT3580-TD1": _lto_media(1),
            "ULT3580-TD2": _lto_media(2),
            "ULT3580-TD3": _lto_media(3),
            "ULT3580-TD4": _lto_media(4),
            "ULT3580-TD5": _lto_media(5),
            "ULT3580-TD6": _lto_media(6),
            "ULT3580-TD7": _lto_media(7),
            "ULT3580-TD8": _lto_media(8),
            "ULT3580-TD9": _lto_media(9),
            "ULT3580-TDA": _lto_media(10),  # LTO10, LTO10P ONLY
        },
        allowed_drive_vendors=["ADIC", "IBM"],
        # All ADIC libraries use IBM LTO drives
        library_drive_mapping={
            "Scalar i2000": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            "Scalar 1000": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
            "scalar": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="A",
    ),

    # ===== SPECTRA =====
    # Libraries: PYTHON, GECKO, 215
    # Drives: SDLT drives
    "SPECTRA": VendorProfile(
        key="SPECTRA",
        library_vendor="SPECTRA",
        library_models=["PYTHON", "GECKO", "215", "GATOR"],
        library_product_default="PYTHON",
        library_revision_default="2000",
        library_supported_media=[
            "LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8",
            "LTO9", "LTO10", "LTO10P",
            "SDLT600", "SDLT320", "SDLT220",
        ],
        library_default_media="LTO8",
        drive_vendor="IBM",
        # Spectra builds libraries, not drives: its T-series and Python
        # libraries carry IBM LTO drives, and the older ones SDLT.
        drive_models=[*IBM_LTO_DRIVES, "SDLT600", "SDLT 320"],
        drive_product_default="ULT3580-TD8",
        drive_revision_default="D.02",
        drive_media_by_model={
            **{drive: _media(drive) for drive in IBM_LTO_DRIVES},
            "SDLT600": _media("SDLT600"),
            "SDLT 320": _media("SDLT 320"),
        },
        allowed_drive_vendors=["SPECTRA", "IBM", "QUANTUM"],
        library_drive_mapping={
            "PYTHON": [*IBM_LTO_DRIVES, "SDLT600", "SDLT 320"],
            "GECKO": [*IBM_LTO_DRIVES, "SDLT600", "SDLT 320"],
            "215": [*IBM_LTO_DRIVES, "SDLT600", "SDLT 320"],
            # init_spectra_gator_smc (usr/pm/spectra_pm.c:105): 32 drives, 645 slots
            "GATOR": [*IBM_LTO_DRIVES, "SDLT600", "SDLT 320"],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="P",
    ),

    # ===== OVERLAND =====
    # Libraries: NEO Series, OVERLAND
    # Drives: LTO drives
    "OVERLAND": VendorProfile(
        key="OVERLAND",
        library_vendor="OVERLAND",
        library_models=["NEO Series", "OVERLAND"],
        library_product_default="NEO Series",
        library_revision_default="0425",
        library_supported_media=[
            "LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8",
            "LTO9", "LTO10", "LTO10P",
        ],
        library_default_media="LTO8",
        drive_vendor="IBM",
        drive_models=list(IBM_LTO_DRIVES),
        drive_product_default="ULT3580-TD8",
        drive_revision_default="D.02",
        drive_media_by_model={drive: _media(drive) for drive in IBM_LTO_DRIVES},
        allowed_drive_vendors=["OVERLAND", "IBM"],
        # Overland builds libraries, not drives; the NEO series carries IBM LTO
        library_drive_mapping={
            "NEO Series": list(IBM_LTO_DRIVES),
            "OVERLAND": list(IBM_LTO_DRIVES),
        },
        default_num_drives=4,
        default_num_maps=1,      # init_overland_smc has one MAP slot
        default_media_count=50,
        barcode_leading="O",
    ),

    # ===== DELL =====
    # Libraries: PV-136T (PowerVault 136T)
    # Drives: Uses IBM ULT3580 LTO drives (Dell does not manufacture drives)
    # Note: MHVTL has no native Dell emulation - uses IBM-compatible drives
    # Original PHP GUI config: PV-136T with ULTRIUM-TD1, revision 54K1, LTO1 only
    # Extended here with modern LTO support while keeping original library/revision values
    "DELL": VendorProfile(
        key="DELL",
        library_vendor="DELL",
        library_models=["PV-136T"],  # From original PHP GUI
        library_product_default="PV-136T",
        library_revision_default="2.60",  # From original PHP GUI
        library_supported_media=["LTO1", "LTO2", "LTO3", "LTO4", "LTO5", "LTO6", "LTO7", "LTO8", "LTO9", "LTO10", "LTO10P"],
        library_default_media="LTO8",
        drive_vendor="IBM",
        drive_models=[
            "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
            "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
        ],
        drive_product_default="ULT3580-TD8",
        drive_revision_default="54K1",  # From original PHP GUI
        drive_media_by_model={
            "ULT3580-TD1": _lto_media(1),
            "ULT3580-TD2": _lto_media(2),
            "ULT3580-TD3": _lto_media(3),
            "ULT3580-TD4": _lto_media(4),
            "ULT3580-TD5": _lto_media(5),
            "ULT3580-TD6": _lto_media(6),
            "ULT3580-TD7": _lto_media(7),
            "ULT3580-TD8": _lto_media(8),
            "ULT3580-TD9": _lto_media(9),
            "ULT3580-TDA": _lto_media(10),  # LTO10, LTO10P ONLY
        },
        allowed_drive_vendors=["DELL", "IBM"],
        # Dell PV-136T uses IBM LTO drives
        library_drive_mapping={
            "PV-136T": [
                "ULT3580-TD1", "ULT3580-TD2", "ULT3580-TD3", "ULT3580-TD4",
                "ULT3580-TD5", "ULT3580-TD6", "ULT3580-TD7", "ULT3580-TD8", "ULT3580-TD9", "ULT3580-TDA",
            ],
        },
        default_num_drives=4,
        default_num_maps=4,
        default_media_count=50,
        barcode_leading="D",  # From original PHP GUI
    ),

    # ===== SONY =====
    # Libraries: LIB-302, LIB-152
    # Drives: AIT (SDX-*) drives
    "SONY": VendorProfile(
        key="SONY",
        library_vendor="SONY",
        library_models=["LIB-302", "LIB-152"],
        library_product_default="LIB-302",
        library_revision_default="0500",
        library_supported_media=["AIT1", "AIT2", "AIT3", "AIT4"],
        library_default_media="AIT4",
        drive_vendor="SONY",
        drive_models=["SDX-300C", "SDX-500C", "SDX-500V", "SDX-700C", "SDX-700V", "SDX-900V"],
        drive_product_default="SDX-900V",
        drive_revision_default="0500",
        drive_media_by_model={
            "SDX-300C": _media("SDX-300C"),
            "SDX-500C": _media("SDX-500C"),
            "SDX-500V": _media("SDX-500V"),
            "SDX-700C": _media("SDX-700C"),
            "SDX-700V": _media("SDX-700V"),
            "SDX-900V": _media("SDX-900V"),
        },
        allowed_drive_vendors=["SONY"],
        # All SONY libraries use SONY AIT drives
        library_drive_mapping={
            "LIB-302": ["SDX-300C", "SDX-500C", "SDX-500V", "SDX-700C", "SDX-700V", "SDX-900V"],
            "LIB-152": ["SDX-300C", "SDX-500C", "SDX-500V", "SDX-700C", "SDX-700V", "SDX-900V"],
        },
        default_num_drives=2,
        default_num_maps=2,
        default_media_count=20,
        barcode_leading="Y",
    ),
}


# ------------------------------------------------------------
# Helper API
# ------------------------------------------------------------

def get_profile(profile_key: Optional[str]) -> VendorProfile:
    """Return a vendor profile by key.

    STRICT: unknown keys raise KeyError; no implicit default.
    """
    if not profile_key:
        raise KeyError("profile_key is required (no default).")
    key = profile_key.strip().upper()
    if key not in PROFILES:
        raise KeyError(f"Unknown/unsupported profile key: {profile_key!r}")
    return PROFILES[key]


def list_profiles() -> List[str]:
    """Return sorted list of supported profile keys."""
    return sorted(PROFILES.keys())


def get_valid_drives_for_library(profile_key: str, library_model: str) -> List[str]:
    """Return list of drives valid for this library model.

    Uses library_drive_mapping for cascading validation.
    If no mapping defined, returns all drives in the profile.
    """
    profile = get_profile(profile_key)
    if profile.library_drive_mapping and library_model in profile.library_drive_mapping:
        return profile.library_drive_mapping[library_model]
    # Fallback: all drives in profile (for profiles without mapping)
    return list(profile.drive_models)


def get_valid_media_for_drive(profile_key: str, drive_model: str) -> List[str]:
    """Return list of media types valid for this drive model.

    Uses drive_media_by_model for strict media compatibility.
    """
    profile = get_profile(profile_key)
    if drive_model in profile.drive_media_by_model:
        return profile.drive_media_by_model[drive_model]
    raise KeyError(f"Unknown drive model for {profile_key}: {drive_model!r}")


def lto_generation(drive_model: str) -> int:
    """10 for ULT3580-TDA, 9 for ULT3580-HH9 or Ultrium 9-SCSI, 0 if not LTO."""
    match = re.search(r'(?:TD|HH)([0-9A])\b|Ultrium (\d+)-', drive_model or '')
    if not match:
        return 0
    value = match.group(1) or match.group(2)
    return 10 if value == 'A' else int(value)


def get_default_drive_for_library(profile_key: str, library_model: str) -> str:
    """The drive a library model gets when none is chosen.

    The profile's own default when this model accepts it; otherwise the newest
    LTO generation the model accepts; otherwise the last drive listed. The rule
    used to be "the last drive listed", on the assumption that the lists run
    oldest to newest - STK L700's ends with DLT7000, and nothing used this
    function, so the profile default (T10000C, which an L700 cannot carry) was
    applied instead and the library failed its own validation.
    """
    profile = get_profile(profile_key)
    valid_drives = get_valid_drives_for_library(profile_key, library_model)
    if not valid_drives:
        raise ValueError(f"No valid drives for library {library_model}")

    if profile.drive_product_default in valid_drives:
        return profile.drive_product_default
    newest = max(valid_drives, key=lto_generation)
    if lto_generation(newest):
        return newest
    return valid_drives[-1]


def get_default_media_for_drive(profile_key: str, drive_model: str) -> str:
    """Return the best default media for a drive model.

    Selects the highest capacity media the drive supports.
    """
    valid_media = get_valid_media_for_drive(profile_key, drive_model)
    if not valid_media:
        raise ValueError(f"No valid media for drive {drive_model}")

    # Return the first media in the list (typically the best/native)
    return valid_media[0]


def get_profile_options(profile_key: str) -> Dict:
    """Return all options for frontend dropdown population.

    Returns a dictionary with:
    - library_models: list of library model options
    - drive_models: list of drive model options (use get_valid_drives_for_library for filtered)
    - media_types: list of media type options (use get_valid_media_for_drive for filtered)
    - defaults: default selections
    """
    profile = get_profile(profile_key)
    return {
        "library_models": list(profile.library_models),
        "drive_models": list(profile.drive_models),
        "media_types": list(profile.library_supported_media),
        "defaults": {
            "library_model": profile.library_product_default,
            "library_revision": profile.library_revision_default,
            "drive_model": profile.drive_product_default,
            "media_type": profile.library_default_media,
            "num_drives": profile.default_num_drives,
            "num_maps": profile.default_num_maps,
            "media_count": profile.default_media_count,
            "empty_slots": profile.default_empty_slots,
        },
        "library_drive_mapping": profile.library_drive_mapping or {},
        "drive_media_mapping": profile.drive_media_by_model or {},
        # What MHVTL will do with each choice, so the form can say so before
        # anything is written (profiles/personalities).
        "default_drive_by_library": {
            model: get_default_drive_for_library(profile_key, model)
            for model in profile.library_models},
        "library_limits": {
            model: _limits(profile.library_vendor, model, profile)
            for model in profile.library_models},
        "read_only_media": {drive: read_only_media(drive)
                            for drive in profile.drive_models},
        "drive_personality": {drive: drive_personality(drive)
                              for drive in profile.drive_models},
    }


def _limits(vendor: str, model: str, profile: VendorProfile) -> Dict:
    """The element limits of the layout MHVTL gives this model, and the
    defaults fitted to them (as services/libraries/spec.apply_defaults does)."""
    layout = library_layout(vendor, model)
    return {"layout": layout.title, "init": layout.init, "source": layout.source,
            "max_drives": layout.max_drives, "max_slots": layout.max_slots,
            "max_maps": layout.max_maps,
            "default_num_drives": min(profile.default_num_drives, layout.max_drives),
            "default_num_maps": min(profile.default_num_maps, layout.max_maps)}


def with_scalar_layout(profile: VendorProfile) -> VendorProfile:
    """The profile, plus a Scalar-layout variant of each of its Scalar models.

    MHVTL emulates the Scalar element layout (usr/pm/scalar_pm.c) only for a
    product beginning ADIC or QUANTUM, so "Scalar i2000" gets the default
    layout. Each variant is a model of its own whose product string does begin
    that way (personalities.scalar_layout_product), carries the same drives as
    the model it comes from, and is created like any other model. Kept apart
    from the hand-written tables so the rule lives in one place.
    """
    models = list(profile.library_models)
    mapping = dict(profile.library_drive_mapping or {})
    for model in profile.library_models:
        variant = scalar_layout_product(profile.library_vendor, model)
        if variant and variant not in models:
            models.append(variant)
            mapping[variant] = list(mapping.get(model, profile.drive_models))
    return replace(profile, library_models=models, library_drive_mapping=mapping)


for _key in ('ADIC', 'QUANTUM'):
    PROFILES[_key] = with_scalar_layout(PROFILES[_key])


def assert_all_profiles_strict() -> None:
    """Run internal consistency checks on all profiles."""
    for p in PROFILES.values():
        p.assert_strict()


# Fail fast in dev/CI if any profile is inconsistent.
assert_all_profiles_strict()
