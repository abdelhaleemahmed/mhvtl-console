"""What MHVTL 1.8 will actually emulate for a given device.conf entry.

A library or drive in device.conf is just a vendor string and a product string.
The daemons decide from those strings which *personality* to emulate - which
inquiry data, which mode pages, and for a library, where in the SCSI element
address space its drives, slots, picker and MAP live. That last part sets hard
limits: a library can only have as many drives as fit between its drive range
and the next range, and vtllibrary silently ignores any drive past that point.

This module is a transcription of those decisions, with a reference to the line
in MHVTL that each one comes from, so that the web UI and the CLI can say before
writing device.conf what MHVTL will do with it. It is checked against the MHVTL
source by tests/test_personalities.py whenever the source tree is present.

Source: the MHVTL tree this project builds and installs,

    1.8-0_release-108-g25c683e   (local, with six patches on top)
    59f32ee                      (upstream master it is based on)

Of the six local patches only 0006 (make_vtl_media's barcode suffixes) touches
anything referenced here; every line number below is the same in both trees
except where two are given.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: The MHVTL tree the references below point into.
MHVTL_REVISION = '1.8-0_release-108-g25c683e'
MHVTL_UPSTREAM_REVISION = '59f32ee'

#: SMC element addresses are two bytes on the wire (usr/smc.c:482,
#: put_unaligned_be16(s->slot_location, ...)). Nothing checks the top-most
#: range against this; it is simply as far as an address can go.
ELEMENT_ADDRESS_SPACE = 0x10000

#: Library and drive ids share one namespace: each id is the daemon's message
#: queue id and the kernel minor number, and both daemons refuse id >= MAXPRIOR
#: (include/utils/q.h:50; usr/cmd/vtllibrary.c:1610; usr/cmd/vtltape.c:2430,
#: upstream 2416; kernel/mhvtl.c:186 DEF_MAX_MINOR_NO).
MAX_DEVICE_ID = 1023

#: LUNs at or above this are rejected by the kernel module
#: (kernel/mhvtl.c:126 DEF_MAX_LUNS, enforced at kernel/mhvtl.c:603).
MAX_LUN = 31

#: device.conf's NAA line is read as eight %02x fields
#: (usr/cmd/vtllibrary.c:1286). Ids and targets are written into it in decimal,
#: so a three-digit value makes MHVTL discard the line and derive an NAA from
#: the serial number instead. Keeping ids and targets to two digits keeps the
#: configured NAA in effect.
MAX_NAA_FIELD = 99

#: vtltape pads the product string to 16 characters (usr/cmd/vtltape.c:2153,
#: upstream 2139)
#: and vtllibrary does the same for 8-character vendors.
VENDOR_ID_LEN = 8
PRODUCT_ID_LEN = 16


@dataclass(frozen=True)
class LibraryLayout:
    """One SMC personality: where each element type starts.

    A range runs from its start to the next higher start. vtllibrary's
    check_overflow() (usr/cmd/vtllibrary.c:704) refuses element n of a type when
    n + start > next start, and the element is addressed at start + n - 1
    (usr/cmd/vtllibrary.c:815), so a type holds exactly (next start - start)
    elements. The top-most type is bounded only by the address width.
    """
    init: str
    title: str
    source: str
    start_picker: int
    start_map: int
    start_drive: int
    start_storage: int

    def _capacity(self, start: int) -> int:
        higher = sorted(value for value in self.starts if value > start)
        return (higher[0] if higher else ELEMENT_ADDRESS_SPACE) - start

    @property
    def starts(self) -> Tuple[int, int, int, int]:
        return (self.start_picker, self.start_map, self.start_drive,
                self.start_storage)

    @property
    def max_drives(self) -> int:
        return self._capacity(self.start_drive)

    @property
    def max_slots(self) -> int:
        return self._capacity(self.start_storage)

    @property
    def max_maps(self) -> int:
        return self._capacity(self.start_map)

    @property
    def max_pickers(self) -> int:
        return self._capacity(self.start_picker)

    def to_dict(self) -> Dict:
        return {'init': self.init, 'title': self.title, 'source': self.source,
                'max_drives': self.max_drives, 'max_slots': self.max_slots,
                'max_maps': self.max_maps, 'max_pickers': self.max_pickers,
                'start': {'picker': self.start_picker, 'map': self.start_map,
                          'drive': self.start_drive,
                          'storage': self.start_storage}}


#: Every SMC layout in MHVTL 1.8, keyed by its init function.
LAYOUTS: Dict[str, LibraryLayout] = {layout.init: layout for layout in (
    LibraryLayout('init_default_smc', 'Default (any library not matched below)',
                  'usr/pm/default_smc_pm.c:53-56', 0x2f0, 0x300, 0x001, 0x400),
    LibraryLayout('init_stkl20', 'STK L20 / L40 / L80',
                  'usr/pm/stklxx_pm.c:62-65', 0x0001, 0x000a, 0x01f4, 0x03e8),
    LibraryLayout('init_stklxx', 'STK L-series (L700, L180, SL3000, ...)',
                  'usr/pm/stklxx_pm.c:87-90', 0x0001, 0x000a, 0x01f4, 0x03e8),
    LibraryLayout('init_stkslxx', 'STK SL500',
                  'usr/pm/stklxx_pm.c:110-113', 0x0001, 0x000a, 0x01f4, 0x03e8),
    LibraryLayout('init_ibmts3100', 'IBM TS3100 / TS3200 (3573-TL)',
                  'usr/pm/ibm_smc_pm.c:319-322', 0x0001, 0x0010, 0x0100, 0x1000),
    LibraryLayout('init_ibm3584', 'IBM 3584',
                  'usr/pm/ibm_smc_pm.c:410-413', 0x0001, 0x0300, 0x0101, 0x0400),
    LibraryLayout('init_hp_eml_smc', 'HP EML / ESL (any HP not MSL)',
                  'usr/pm/hp_smc_pm.c:101-104', 0x0001, 0x000a, 0x01f4, 0x03e8),
    LibraryLayout('init_hp_msl_smc', 'HP MSL',
                  'usr/pm/hp_smc_pm.c:127-130', 0x0001, 0x01c0, 0x01e0, 0x0020),
    LibraryLayout('init_overland_smc', 'Overland',
                  'usr/pm/overland_pm.c:106-109', 0x0001, 0x0000, 0x00ff, 0x0002),
    LibraryLayout('init_scalar_smc', 'ADIC / Quantum Scalar',
                  'usr/pm/scalar_pm.c:109-112', 0x0001, 0x0010, 0x0100, 0x1000),
    LibraryLayout('init_spectra_logic_smc', 'Spectra Logic (Python, T-series)',
                  'usr/pm/spectra_pm.c:138-141', 0x0001, 0x0010, 0x0100, 0x1000),
    LibraryLayout('init_spectra_gator_smc', 'Spectra Gator',
                  'usr/pm/spectra_pm.c:113-116', 0x02c3, 0x0001, 0x02a3, 0x001e),
    LibraryLayout('init_spectra_215_smc', 'Spectra 215',
                  'usr/pm/spectra_pm.c:87-90', 86, 99, 31, 1),
)}


#: vtllibrary's selector, in order: (field, prefix, then-function or
#: sub-selector). Matching is case-insensitive over the prefix's length -
#: strncasecmp - against the vendor or product as read from device.conf.
#: usr/cmd/vtllibrary.c:1461 customise_lu and the customise_*_lu functions at
#: 1421 (IBM), 1430 (STK), 1443 (HP) and 1450 (Spectra).
#:
#: Worth knowing: ADIC, QUANTUM and OVERLAND are matched on the *product*, not
#: the vendor. Upstream changed this in 24f7342 ("library: Fix vendor 'QUANTUM'
#: not 'SCALAR'", for issue #152, where a product of "Scalar i2000" misbehaved
#: under the Scalar personality). So vendor QUANTUM with product "Scalar i500" -
#: what a real Quantum library reports - gets the default layout, and the Scalar
#: layout is reached only by a product beginning "ADIC" or "QUANTUM".
LIBRARY_SELECTOR: List[Tuple[str, str, object]] = [
    ('vendor', 'stk', [
        ('product', 'SL500', 'init_stkslxx'),
        ('product', 'L20', 'init_stkl20'),
        ('product', 'L40', 'init_stkl20'),
        ('product', 'L80', 'init_stkl20'),
        (None, None, 'init_stklxx'),
    ]),
    ('vendor', 'IBM', [
        ('product', '3573-TL', 'init_ibmts3100'),
        ('product', '03584', 'init_ibm3584'),
        (None, None, 'init_default_smc'),
    ]),
    ('vendor', 'HP', [
        ('product', 'MSL', 'init_hp_msl_smc'),
        (None, None, 'init_hp_eml_smc'),
    ]),
    ('product', 'OVERLAND', 'init_overland_smc'),
    ('product', 'ADIC', 'init_scalar_smc'),
    ('product', 'QUANTUM', 'init_scalar_smc'),
    ('vendor', 'SPECTRA', [
        ('product', 'PYTHON', 'init_spectra_logic_smc'),
        ('product', 'GATOR', 'init_spectra_gator_smc'),
        ('product', '215', 'init_spectra_215_smc'),
        (None, None, 'init_spectra_logic_smc'),
    ]),
    (None, None, 'init_default_smc'),
]


def _matches(field: Optional[str], prefix: Optional[str], vendor: str,
             product: str) -> bool:
    if field is None:
        return True
    value = vendor if field == 'vendor' else product
    return value[:len(prefix)].lower() == prefix.lower()


#: The product prefixes that select init_scalar_smc (vtllibrary.c:1470-1473).
SCALAR_PRODUCT_PREFIXES = ('ADIC', 'QUANTUM')


def scalar_layout_product(vendor: str, model: str) -> Optional[str]:
    """A product string that gets the Scalar layout, for a Scalar model.

    MHVTL gives init_scalar_smc only to a product beginning ADIC or QUANTUM,
    which no real Scalar reports ("Scalar i2000"), so a profile model never
    reaches it. The variant puts the vendor in front of the model - dropping
    the word "Scalar" when that is needed to fit the 16-character product
    field - and returns None for a vendor that is not one of the two or a
    model that is not a Scalar.

        ADIC,    Scalar 1000   ->  "ADIC Scalar 1000"
        ADIC,    Scalar i2000  ->  "ADIC i2000"
        QUANTUM, Scalar i500   ->  "QUANTUM i500"
    """
    vendor = (vendor or '').upper()
    if vendor not in SCALAR_PRODUCT_PREFIXES or 'scalar' not in (model or '').lower():
        return None
    for candidate in (f'{vendor} {model}',
                      f'{vendor} {model[len("scalar"):].strip()}'
                      if model.lower().startswith('scalar ') else None):
        if candidate and len(candidate) <= PRODUCT_ID_LEN:
            return candidate
    return None


def library_layout(vendor: str, product: str) -> LibraryLayout:
    """The layout vtllibrary will use for this vendor and product."""
    vendor = (vendor or '')[:VENDOR_ID_LEN]
    product = (product or '')[:PRODUCT_ID_LEN]
    for field, prefix, outcome in LIBRARY_SELECTOR:
        if not _matches(field, prefix, vendor, product):
            continue
        if isinstance(outcome, str):
            return LAYOUTS[outcome]
        for sub_field, sub_prefix, init in outcome:
            if _matches(sub_field, sub_prefix, vendor, product):
                return LAYOUTS[init]
    return LAYOUTS['init_default_smc']


#: vtltape's table, usr/cmd/vtltape.c:132 (tape_drives[]), matched by
#: config_lu() at usr/cmd/vtltape.c:1899 (local; upstream 1885): an exact,
#: case-sensitive comparison of the product padded to 16 characters. A product
#: not in this table is emulated as a generic drive (init_default_ssc), which
#: backup software will not recognise as the model it was told to expect.
DRIVE_PERSONALITIES: Dict[str, str] = {
    **{f'ULT3580-TD{g}': f'init_ult3580_td{g}' for g in '123456789'},
    'ULT3580-TDA': 'init_ult3580_tda',
    **{f'ULT3580-HH{g}': f'init_ult3580_td{g}' for g in '789'},
    'ULT3580-HHA': 'init_ult3580_tda',
    **{f'ULTRIUM-TD{g}': f'init_ult3580_td{g}' for g in '123456789'},
    'ULTRIUM-TDA': 'init_ult3580_tda',
    **{f'ULTRIUM-HH{g}': f'init_ult3580_td{g}' for g in '23456789'},
    'ULTRIUM-HHA': 'init_ult3580_tda',
    **{f'Ultrium {g}-SCSI': f'init_hp_ult_{g}' for g in '12345678'},
    'SDX-300C': 'init_ait1_ssc',
    'SDX-500C': 'init_ait2_ssc',
    'SDX-500V': 'init_ait2_ssc',
    'SDX-700C': 'init_ait3_ssc',
    'SDX-700V': 'init_ait3_ssc',
    'SDX-900V': 'init_ait4_ssc',
    '03592J1A': 'init_3592_j1a',
    '03592E05': 'init_3592_E05',
    '03592E06': 'init_3592_E06',
    '03592E07': 'init_3592_E07',
    'T10000C': 'init_t10kC_ssc',
    'T10000B': 'init_t10kB_ssc',
    'T10000A': 'init_t10kA_ssc',
    'T9840D': 'init_9840D_ssc',
    'T9840C': 'init_9840C_ssc',
    'T9840B': 'init_9840B_ssc',
    'T9840A': 'init_9840A_ssc',
    'T9940B': 'init_9940B_ssc',
    'T9940A': 'init_9940A_ssc',
    'DLT7000': 'init_dlt7000_ssc',
    'DLT8000': 'init_dlt8000_ssc',
    'SDLT 320': 'init_sdlt320_ssc',
    'SDLT600': 'init_sdlt600_ssc',
}

GENERIC_DRIVE = 'init_default_ssc'


def drive_personality(product: str) -> str:
    """The personality vtltape will emulate for this product string."""
    padded = f'{(product or "")[:PRODUCT_ID_LEN]:<{PRODUCT_ID_LEN}}'
    return DRIVE_PERSONALITIES.get(padded.rstrip(), GENERIC_DRIVE)


# -- media ------------------------------------------------------------------
#
# Media are named here the way mktape -d names them, because that is the only
# name MHVTL accepts when a cartridge is made: set_media_params()
# (usr/vtllib.c:1826) compares against these strings and prints "'%s' is an
# invalid density" for anything else. The profiles used to offer T10000A,
# DLT7000 and DLT8000, which are drive names, not densities - a library built
# on them never got its tapes.

@dataclass(frozen=True)
class MediaSupport:
    """The cartridges a drive personality loads, and whether it can write them.

    Transcribed from add_drive_media_list(lu, LOAD_RW | LOAD_RO, ...) in each
    init_* function, and for LTO-7 and later from the tdN_media tables. What
    a drive does not list, it refuses: vtltape unloads the cartridge again
    (usr/cmd/vtltape.c:1512, "Load failed: Data format not suitable") and
    leaves it in the drive for someone to unmount.
    """
    read_write: Tuple[str, ...]
    read_only: Tuple[str, ...] = ()

    @property
    def loads(self) -> Tuple[str, ...]:
        """Everything the drive accepts, native first."""
        return self.read_write + self.read_only


def _lto(generation: int) -> MediaSupport:
    """LTO-1 .. LTO-10 as usr/pm/ult3580_pm.c and hp_ultrium_pm.c have them.

        LTO-1         LTO1
        LTO-2         LTO2, LTO1
        LTO-3 .. 7    LTOn, LTOn-1 read/write; LTOn-2 read-only
        LTO-8, 9      LTOn, LTOn-1                (ult3580_pm.c:987-1017)
        LTO-10        LTO10, LTO10P               (ult3580_pm.c:1019-1024)
    """
    if generation == 1:
        return MediaSupport(('LTO1',))
    if generation == 2:
        return MediaSupport(('LTO2', 'LTO1'))
    if generation in (8, 9):
        return MediaSupport((f'LTO{generation}', f'LTO{generation - 1}'))
    if generation == 10:
        return MediaSupport(('LTO10', 'LTO10P'))
    return MediaSupport((f'LTO{generation}', f'LTO{generation - 1}'),
                        (f'LTO{generation - 2}',))


#: Drive personality -> what it loads. Cleaning and WORM entries are left out:
#: a WORM cartridge is the same density made with mktape -t WORM.
DRIVE_MEDIA: Dict[str, MediaSupport] = {
    **{f'init_ult3580_td{g}': _lto(g) for g in range(1, 10)},
    'init_ult3580_tda': _lto(10),
    **{f'init_hp_ult_{g}': _lto(g) for g in range(1, 9)},
    # usr/pm/ait_pm.c
    'init_ait1_ssc': MediaSupport(('AIT1',)),
    'init_ait2_ssc': MediaSupport(('AIT2', 'AIT1')),
    'init_ait3_ssc': MediaSupport(('AIT3', 'AIT2'), ('AIT1',)),
    'init_ait4_ssc': MediaSupport(('AIT4', 'AIT3'), ('AIT2',)),
    # usr/pm/quantum_dlt_pm.c:374-376, 425-427, 476-479, 530-535
    'init_dlt7000_ssc': MediaSupport(('DLT4',), ('DLT3',)),
    'init_dlt8000_ssc': MediaSupport(('DLT4',), ('DLT3',)),
    'init_sdlt320_ssc': MediaSupport(('SDLT320', 'SDLT220'), ('SDLT1',)),
    'init_sdlt600_ssc': MediaSupport(('SDLT600', 'SDLT320'), ('SDLT220',)),
    # usr/pm/stk9x40_pm.c:377, 416-419, 459-464, 501-506, 540, 577-580
    'init_9840A_ssc': MediaSupport(('9840A',)),
    'init_9840B_ssc': MediaSupport(('9840B', '9840A')),
    'init_9840C_ssc': MediaSupport(('9840C', '9840B'), ('9840A',)),
    'init_9840D_ssc': MediaSupport(('9840D', '9840C'), ('9840B',)),
    'init_9940A_ssc': MediaSupport(('9940A',)),
    'init_9940B_ssc': MediaSupport(('9940B', '9940A')),
    # usr/pm/t10000_pm.c:460, 500-503, 545-550
    'init_t10kA_ssc': MediaSupport(('T10KA',)),
    'init_t10kB_ssc': MediaSupport(('T10KB', 'T10KA')),
    'init_t10kC_ssc': MediaSupport(('T10KC', 'T10KB', 'T10KA')),
    # usr/pm/ibm_03592_pm.c; each density is the generation that writes it
    # (J1A -> "03592 JA", E05 -> JB, E06 -> JC, E07 -> JK: set_media_params
    # in usr/vtllib.c and the media_info table at ibm_03592_pm.c:57-83)
    'init_3592_j1a': MediaSupport(('J1A',)),
    'init_3592_E05': MediaSupport(('E05', 'J1A')),
    'init_3592_E06': MediaSupport(('E06', 'E05', 'J1A')),
    'init_3592_E07': MediaSupport(('E07', 'E06', 'E05')),
}

#: Density -> the two-character barcode suffix a data cartridge carries, as
#: make_vtl_media reads it back (usr/cmd/make_vtl_media.in:81-168, upstream
#: 81-164) and etc/generate_library_contents.in:66-90 documents it. A density
#: with no suffix here cannot be named by a barcode, so it cannot be created:
#: DLT3 and SDLT1 exist only as read-only legacy media.
#:
#: Upstream make_vtl_media cannot read three of these back: its suffix pattern
#: (make_vtl_media.in:91 upstream) has no C or T in the second position, so TC
#: (T10KC) and LT (LTO-3 WORM) come out UNKNOWN, and it has no branch for JC
#: (E06), though the legend documents it. The local tree carries the fix
#: (patches/1.8-upstream-submission/0006). This app passes the density to
#: mktape itself, so all three work here either way; an unpatched hand-run
#: make_vtl_media stops at the first such tape. tests/test_media checks both.
SUFFIX_BY_DENSITY: Dict[str, str] = {
    **{f'LTO{g}': f'L{g}' for g in range(1, 10)},
    'LTO10': 'LA', 'LTO10P': 'PA',
    'AIT1': 'X1', 'AIT2': 'X2', 'AIT3': 'X3', 'AIT4': 'X4',
    'DLT4': 'D7',
    'SDLT220': 'S1', 'SDLT320': 'S2', 'SDLT600': 'S3',
    '9840A': 'TZ', '9840B': 'TY', '9840C': 'TX', '9840D': 'TW',
    '9940A': 'TV', '9940B': 'TU',
    'T10KA': 'TA', 'T10KB': 'TB', 'T10KC': 'TC',
    'J1A': 'JA', 'E05': 'JB', 'E06': 'JC', 'E07': 'JK',
}

#: Suffix -> density, including the WORM suffixes, which name the same
#: density as the data cartridge. The 3592 WORM suffixes follow
#: make_vtl_media (JW and JX -> E05, JY -> E07), not the comment beside them.
DENSITY_BY_SUFFIX: Dict[str, str] = {
    **{suffix: density for density, suffix in SUFFIX_BY_DENSITY.items()},
    'LT': 'LTO3', 'LU': 'LTO4', 'LV': 'LTO5', 'LW': 'LTO6',
    'LX': 'LTO7', 'LY': 'LTO8', 'LZ': 'LTO9', 'LH': 'LTO10',
    'JW': 'E05', 'JX': 'E05', 'JY': 'E07',
}


#: Density -> native capacity in GB (SI), from media_native_capacity()
#: (usr/vtllib.c:1700). mktape -s 0 creates a tape of exactly this size. The
#: 9840 and 9940 media are not in that switch and fall to its 1 GB default, so
#: they are left out here and a size should always be given for them.
NATIVE_CAPACITY_GB: Dict[str, int] = {
    'LTO1': 100, 'LTO2': 200, 'LTO3': 400, 'LTO4': 800, 'LTO5': 1500,
    'LTO6': 2500, 'LTO7': 6000, 'LTO8': 12000, 'LTO9': 18000,
    'LTO10': 30000, 'LTO10P': 40000,
    'J1A': 300, 'E05': 700, 'E06': 1000, 'E07': 4000,
    'T10KA': 500, 'T10KB': 1000, 'T10KC': 5000,
    'AIT1': 35, 'AIT2': 50, 'AIT3': 100, 'AIT4': 200,
    'DLT3': 10, 'DLT4': 20,
    'SDLT1': 100, 'SDLT220': 110, 'SDLT320': 160, 'SDLT600': 300,
}


def media_label(density: str) -> str:
    """'LTO8 (12 TB)', for a drop-down."""
    size = NATIVE_CAPACITY_GB.get(density)
    if not size:
        return density
    return f'{density} ({size / 1000:g} TB)' if size >= 1000 else f'{density} ({size} GB)'


#: LTO density -> the suffix of its WORM cartridge (generate_library_contents.in:69-74).
WORM_SUFFIX_BY_DENSITY: Dict[str, str] = {
    'LTO3': 'LT', 'LTO4': 'LU', 'LTO5': 'LV', 'LTO6': 'LW',
    'LTO7': 'LX', 'LTO8': 'LY', 'LTO9': 'LZ', 'LTO10': 'LH',
}


def suffix_for(density: str, kind: str = 'data') -> Optional[str]:
    """The barcode suffix for a density: the WORM one for a WORM LTO tape."""
    density = (density or '').upper()
    if (kind or '').upper() == 'WORM' and density in WORM_SUFFIX_BY_DENSITY:
        return WORM_SUFFIX_BY_DENSITY[density]
    return SUFFIX_BY_DENSITY.get(density)


def drive_media(product: str) -> Optional[MediaSupport]:
    """What a drive with this product string loads, or None for the generic
    personality, which MHVTL gives no media list at all."""
    return DRIVE_MEDIA.get(drive_personality(product))


def creatable_media(product: str) -> List[str]:
    """The media a library with this drive can be filled with.

    Everything the drive loads that a barcode can name, native first. The
    read-only generation is included - a restore library is a real thing to
    build - and validation warns about it.
    """
    support = drive_media(product)
    if support is None:
        return []
    return [m for m in support.loads if m in SUFFIX_BY_DENSITY]


def read_only_media(product: str) -> List[str]:
    """The media this drive loads but cannot write."""
    support = drive_media(product)
    return list(support.read_only) if support else []


def density_for_barcode(barcode: str) -> Optional[str]:
    """The density a barcode's last two characters declare, or None."""
    barcode = (barcode or '').strip().upper()
    if len(barcode) < 2:
        return None
    return DENSITY_BY_SUFFIX.get(barcode[-2:])


def media_verdict(density: Optional[str], product: str) -> Dict:
    """Whether a drive with this product string loads this density, and how.

    {'known': bool, 'can_read': bool, 'can_write': bool}. known is False when
    either side is unrecognised: MHVTL decides on load, and the caller should
    say so rather than refuse.
    """
    support = drive_media(product)
    if not density or support is None:
        return {'known': False, 'can_read': True, 'can_write': True}
    return {'known': True,
            'can_read': density in support.loads,
            'can_write': density in support.read_write}


def limits_problems(vendor: str, product: str, *, drives: int = 0,
                    slots: int = 0, maps: int = 0, pickers: int = 1) -> List[str]:
    """Everything about these counts that MHVTL would silently not emulate."""
    layout = library_layout(vendor, product)
    problems = []
    for label, count, limit in (('drives', drives, layout.max_drives),
                                ('storage slots', slots, layout.max_slots),
                                ('MAP slots', maps, layout.max_maps),
                                ('pickers', pickers, layout.max_pickers)):
        if count > limit:
            problems.append(
                f'{layout.title} can hold at most {limit} {label}; {count} '
                f'requested. MHVTL would ignore the rest ({layout.source}).')
    return problems
