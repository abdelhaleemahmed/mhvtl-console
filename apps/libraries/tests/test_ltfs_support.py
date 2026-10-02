"""services/profiles/ltfs_support.py and services/tapes/ltfs_state.py.

profiles/ltfs_support.py pins tables that live in a second upstream project. Pinning
another project's tables is only defensible with a test that fails when they
move - without one the module is a comment that claims to be code.

Skipped when the LTFS source is not beside this project, the same way
test_personalities.py skips without the MHVTL source. Point LTFS_SOURCE at a
checkout to run it elsewhere.
"""
import json
import os
import re
import unittest
from pathlib import Path

from django.test import TestCase

from apps.libraries.services.profiles import ltfs_support as ltfs
from apps.libraries.services.profiles import personalities

GUI_ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(os.environ.get('LTFS_SOURCE', GUI_ROOT.parent / 'ltfs'))
HAVE_SOURCE = (SOURCE / 'src' / 'tape_drivers' / 'vendor_compat.c').exists()

#: The vendor-id macro each table is keyed by, so a TAPEDRIVE row can be
#: attributed to the vendor string LTFS compares against.
VENDOR_MACROS = {
    'IBM_VENDOR_ID': 'IBM',
    'HP_VENDOR_ID': 'HP',
    'HPE_VENDOR_ID': 'HPE',
    'QUANTUM_VENDOR_ID': 'QUANTUM',
    'TANDBERG_VENDOR_ID': 'TANDBERG',
}

TAPEDRIVE = re.compile(
    r'TAPEDRIVE\(\s*(\w+)\s*,\s*"([^"]*)"\s*,\s*DRIVE_(\w+)\s*,')


def read(relative: str) -> str:
    return (SOURCE / relative).read_text(errors='replace')


def source_drives() -> dict:
    """{vendor: {product: family}} as the LTFS tables declare it."""
    found: dict = {}
    for name in ('ibm_tape.c', 'hp_tape.c', 'quantum_tape.c'):
        for macro, product, family in TAPEDRIVE.findall(
                read(f'src/tape_drivers/{name}')):
            vendor = VENDOR_MACROS[macro]
            found.setdefault(vendor, {})[product.strip()] = family
    return found


@unittest.skipUnless(HAVE_SOURCE, f'LTFS source not found at {SOURCE}')
class LtfsSourceTests(TestCase):
    """The transcription against the source it transcribes."""

    def test_every_ltfs_vendor_id_matches_the_source(self):
        """get_vendor_id() decides which vendors have a table at all."""
        body = read('src/tape_drivers/vendor_compat.c')
        block = body[body.index('int get_vendor_id('):]
        block = block[:block.index('\n}')]
        # Each branch compares against a *_VENDOR_ID macro; collect them.
        macros = set(re.findall(r'strncmp\(vendor,\s*(\w+_VENDOR_ID)', block))
        reachable = {VENDOR_MACROS[m] for m in macros if m in VENDOR_MACROS}
        self.assertEqual(reachable, set(ltfs.KNOWN_VENDORS),
                         'KNOWN_VENDORS has drifted from get_vendor_id()')

    def test_a_vendor_with_a_table_but_no_branch_stays_unreachable(self):
        """TANDBERG has entries and no branch, so it can never match."""
        declared = set(source_drives())
        self.assertTrue(set(ltfs.UNREACHABLE_VENDORS) <= declared,
                        'UNREACHABLE_VENDORS names a vendor with no table rows')
        for vendor in ltfs.UNREACHABLE_VENDORS:
            self.assertNotIn(vendor, ltfs.KNOWN_VENDORS)
            self.assertNotIn(vendor, ltfs.SUPPORTED_DRIVES)

    def test_every_supported_drive_matches_the_source(self):
        """Both directions: nothing invented, nothing missed."""
        declared = source_drives()
        for vendor in ltfs.KNOWN_VENDORS:
            with self.subTest(vendor=vendor):
                self.assertEqual(ltfs.SUPPORTED_DRIVES[vendor],
                                 declared.get(vendor, {}))

    def test_the_firmware_minimums_match_the_source(self):
        """The one that matters: it is what makes an LTO-8 drive unopenable."""
        header = read('src/tape_drivers/ibm_tape.h')
        for family, expected in ltfs.FIRMWARE_MINIMUM['IBM'].items():
            with self.subTest(family=family):
                pattern = (r'base_firmware_level_' + family.lower()
                           + r'\[\]\s*=\s*"([^"]+)"')
                match = re.search(pattern, header)
                self.assertIsNotNone(
                    match, f'no base_firmware_level_{family.lower()} in ibm_tape.h')
                self.assertEqual(match.group(1), expected)

    def test_only_ibm_drives_are_gated_on_firmware(self):
        """The check is dispatched on the VENDOR, and IBM is the only case.

        This is the test that was missing. The one below asks which drive
        *families* are gated and never asked which vendors, so a table that
        applied IBM's minimums to HP, HPE and Quantum passed for months.
        """
        body = read('src/tape_drivers/vendor_compat.c')
        block = body[body.index('bool drive_has_supported_fw('):]
        block = block[:block.index('\n}')]

        called_for = set()
        pending = []
        for line in block.splitlines():
            case = re.match(r'\s*case (VENDOR_\w+):', line)
            if case:
                pending.append(case.group(1))
            elif '_is_supported_firmware(' in line:
                called_for.update(pending)
                pending = []
            elif re.match(r'\s*(break;|default:)', line):
                pending = []

        self.assertEqual(called_for, {'VENDOR_IBM'},
                         'a vendor gained or lost a firmware check upstream')
        self.assertEqual(set(ltfs.FIRMWARE_MINIMUM), {'IBM'},
                         'our table must carry a row only for a vendor upstream '
                         'actually checks')

    def test_no_other_vendor_has_a_firmware_function_to_call(self):
        """Why the default branch returns true rather than being a bug."""
        for name in ('hp', 'quantum'):
            with self.subTest(vendor=name):
                self.assertNotIn(f'{name}_tape_is_supported_firmware',
                                 read(f'src/tape_drivers/{name}_tape.c'))

    def test_only_these_families_are_gated_on_firmware(self):
        """Every other generation falls through to `default: break`."""
        body = read('src/tape_drivers/ibm_tape.c')
        block = body[body.index('bool ibm_tape_is_supported_firmware('):]
        block = block[:block.index('\n}')]
        # A family is gated iff its case block mentions base_firmware_level.
        gated, pending = set(), []
        for line in block.splitlines():
            case = re.match(r'\s*case DRIVE_(\w+):', line)
            if case:
                pending.append(case.group(1))
            elif 'base_firmware_level' in line:
                gated.update(pending)
                pending = []
            elif re.match(r'\s*(break;|default:)', line):
                pending = []
        ours = {f for f in ltfs.FIRMWARE_MINIMUM['IBM']
                for f in (f, f + '_HH')} & gated
        self.assertEqual(
            {f.replace('_HH', '') for f in gated},
            set(ltfs.FIRMWARE_MINIMUM['IBM']),
            'a drive family gained or lost a firmware minimum')
        self.assertTrue(ours, 'sanity: our families appear in the switch')


class LtfsMediaTests(TestCase):
    """Which cartridges could become an LTFS volume in a given drive.

    Three conditions at once, and the second is the one that gets forgotten:
    the generation can be partitioned, THE DRIVE CAN WRITE IT, and a barcode
    can name it. Deliberately not asked here: whether LTFS opens the drive -
    that is supports(), and a caller checks both.
    """

    def densities(self, product):
        return ltfs.ltfs_densities_for(product)

    def reason_for(self, product, density):
        for medium in ltfs.ltfs_media_for(product):
            if medium.density == density:
                return medium.reason
        raise AssertionError(f'{density} not offered for {product}')

    def test_an_lto8_drive_offers_lto8_and_lto7(self):
        self.assertEqual(self.densities('ULT3580-TD8'), ['LTO8', 'LTO7'])

    def test_native_density_comes_first_so_it_can_be_the_default(self):
        self.assertEqual(self.densities('ULT3580-TD7')[0], 'LTO7')

    def test_a_read_only_generation_is_refused_because_mkltfs_writes(self):
        """An ULT3580-TD7 loads LTO5 read-only. The medium can be partitioned;
        this drive cannot write it, so no volume can be made in it."""
        self.assertNotIn('LTO5', self.densities('ULT3580-TD7'))
        self.assertIn('read-only', self.reason_for('ULT3580-TD7', 'LTO5'))
        self.assertIn('mkltfs writes', self.reason_for('ULT3580-TD7', 'LTO5'))

    def test_lto4_and_older_are_refused_whatever_the_drive(self):
        """LTO-5 introduced partitioning. An LTO-5 drive writes LTO-4 happily
        and no LTFS volume can live on it."""
        self.assertIn('LTO5', self.densities('ULT3580-TD5'))
        self.assertNotIn('LTO4', self.densities('ULT3580-TD5'))
        self.assertIn('cannot be partitioned',
                      self.reason_for('ULT3580-TD5', 'LTO4'))

    def test_a_3592_drive_offers_its_own_media(self):
        """LTFS's tables carry TS1140 (03592E07) and later, and IBM ships LTFS
        for them, so 3592 densities are usable - the generation is not in the
        name, so there is no arithmetic to do and a set says which."""
        self.assertEqual(self.densities('03592E07'), ['E07', 'E06', 'E05'])

    def test_a_drive_ltfs_never_opens_offers_nothing(self):
        """A T10000 writes T10K media, which no LTFS table covers. The reason
        is stated rather than the density silently dropped."""
        self.assertEqual(self.densities('T10000C'), [])
        self.assertIn('not established', self.reason_for('T10000C', 'T10KC'))

    def test_an_unknown_model_offers_nothing_rather_than_guessing(self):
        """MHVTL emulates it as a generic drive with no media list at all."""
        self.assertEqual(ltfs.ltfs_media_for('NO-SUCH-DRIVE'), [])
        self.assertEqual(self.densities('NO-SUCH-DRIVE'), [])

    def test_every_row_carries_the_barcode_suffix(self):
        for medium in ltfs.ltfs_media_for('ULT3580-TD7'):
            with self.subTest(density=medium.density):
                self.assertEqual(
                    medium.suffix,
                    personalities.SUFFIX_BY_DENSITY[medium.density])

    def test_a_usable_row_gives_no_reason_and_a_refusal_always_does(self):
        for medium in ltfs.ltfs_media_for('ULT3580-TD5'):
            with self.subTest(density=medium.density):
                self.assertEqual(bool(medium.reason), not medium.usable)

    def test_to_dict_is_json_safe(self):
        row = ltfs.ltfs_media_for('ULT3580-TD8')[0].to_dict()
        self.assertEqual(sorted(row), ['density', 'reason', 'suffix', 'usable'])
        json.dumps(row)

    def test_the_partition_floor_matches_the_cartridge_reader(self):
        """One rule, two places that need it: this table and the gate that
        decides which cartridges get read at all."""
        from apps.libraries.services.tapes import ltfs_state

        self.assertEqual(ltfs.FIRST_PARTITIONABLE_LTO,
                         ltfs_state.FIRST_PARTITIONABLE_LTO)


class LtfsSupportTests(TestCase):
    """The answers, without needing the source present."""

    def test_an_ibm_lto7_drive_is_supported_with_any_revision(self):
        result = ltfs.supports('IBM', 'ULT3580-TD7', 'D.02')
        self.assertTrue(result.supported)
        self.assertEqual(result.family, 'LTO7')
        self.assertIsNone(result.firmware_minimum)

    def test_an_ibm_lto8_drive_is_refused_below_hb81(self):
        """What this console actually writes today, and why it cannot be used."""
        result = ltfs.supports('IBM', 'ULT3580-TD8', 'D.02')
        self.assertFalse(result.supported)
        self.assertEqual(result.family, 'LTO8')
        self.assertEqual(result.firmware_minimum, 'HB81')
        self.assertIn('HB81', result.reason)

    def test_an_ibm_lto8_drive_is_accepted_at_or_above_hb81(self):
        for revision in ('HB81', 'HB82', 'J4H0'):
            with self.subTest(revision=revision):
                self.assertTrue(
                    ltfs.supports('IBM', 'ULT3580-TD8', revision).supported)

    def test_an_hpe_lto8_drive_is_accepted_at_any_revision(self):
        """The bug this pair exists for: the gate is IBM's, not LTO-8's.

        Our table was vendor-blind and reported that an HPE Ultrium 8 at 'D.02'
        would be refused. LTFS accepts it - drive_has_supported_fw() checks
        firmware for VENDOR_IBM and returns true for everyone else
        (vendor_compat.c:347).
        """
        result = ltfs.supports('HPE', 'Ultrium 8-SCSI', 'D.02')
        self.assertTrue(result.supported, result.reason)
        self.assertEqual(result.family, 'LTO8')
        self.assertIsNone(result.firmware_minimum,
                          'an HPE drive has no gate to report')

    def test_the_same_revision_is_refused_on_ibm_and_accepted_on_hpe(self):
        """One revision, two answers, decided by the vendor id alone."""
        self.assertFalse(ltfs.supports('IBM', 'ULT3580-TD8', 'D.02').supported)
        self.assertTrue(ltfs.supports('HPE', 'Ultrium 8-SCSI', 'D.02').supported)

    def test_a_quantum_drive_has_no_gate(self):
        result = ltfs.supports('QUANTUM', 'ULTRIUM-HH8', 'AAAA')
        self.assertTrue(result.supported, result.reason)
        self.assertIsNone(result.firmware_minimum)

    def test_the_minimum_is_None_for_a_vendor_with_no_table(self):
        self.assertIsNone(ltfs.firmware_minimum_for('LTO8', 'HPE'))
        self.assertIsNone(ltfs.firmware_minimum_for('LTO8', 'QUANTUM'))
        self.assertIsNone(ltfs.firmware_minimum_for('LTO8', ''))
        self.assertEqual(ltfs.firmware_minimum_for('LTO8', 'IBM'), 'HB81')

    def test_a_half_height_drive_shares_its_siblings_minimum(self):
        self.assertEqual(ltfs.firmware_minimum_for('LTO8_HH', 'IBM'), 'HB81')
        self.assertEqual(ltfs.firmware_minimum_for('LTO8', 'IBM'), 'HB81')
        self.assertIsNone(ltfs.firmware_minimum_for('LTO7_HH', 'IBM'))

    def test_an_unknown_vendor_is_refused_and_said_to_be_unknown(self):
        """STK + an IBM product id: what MHVTL's STK profile produces, and what
        crashed LTFS before our patch."""
        result = ltfs.supports('STK', 'ULT3580-TD8', 'D.02')
        self.assertFalse(result.supported)
        self.assertTrue(result.vendor_unknown)
        self.assertIsNone(result.family)
        self.assertIn('STK', result.reason)

    def test_a_known_vendor_with_an_unknown_product_is_not_vendor_unknown(self):
        result = ltfs.supports('IBM', 'T10000B', 'D.02')
        self.assertFalse(result.supported)
        self.assertFalse(result.vendor_unknown)

    def test_padding_is_ignored_the_way_ltfs_ignores_it(self):
        self.assertTrue(ltfs.supports('IBM     ', 'ULT3580-TD7     ').supported)

    def test_no_revision_reports_the_gate_without_applying_it(self):
        """A catalogue listing wants 'this model is capable'."""
        result = ltfs.supports('IBM', 'ULT3580-TD8')
        self.assertTrue(result.supported)
        self.assertEqual(result.firmware_minimum, 'HB81')

    def test_usable_drive_models_is_empty_for_an_unknown_vendor(self):
        self.assertEqual(ltfs.usable_drive_models('STK'), {})
        self.assertIn('ULT3580-TD8', ltfs.usable_drive_models('IBM'))

    def test_to_dict_is_json_safe(self):
        import json
        json.dumps(ltfs.supports('IBM', 'ULT3580-TD8', 'D.02').to_dict())


# --------------------------------------------------------------------------
# services/tapes/ltfs.py - the cartridge reader
# --------------------------------------------------------------------------
import struct as _struct

from apps.libraries.services.tapes import ltfs_state as tape_ltfs

MEDIA_ROOT = Path('/opt/mhvtl')
HAVE_MEDIA = MEDIA_ROOT.is_dir()


def tlv(records, header=0) -> bytes:
    """Build a type-length-value blob the way mhvtl writes one.

    ids and lengths little-endian, values exactly as given - so a test that
    wants a big-endian value has to say so, which is the point.
    """
    out = bytearray(b'\0' * header)
    for attribute, value in records:
        out += _struct.pack('<HH', attribute, len(value)) + value
    return bytes(out)


def be(value: int, width: int = 8) -> bytes:
    return value.to_bytes(width, 'big')


def vtl_blob(partitions=1, capacities=(0, 0), coherency_p1=False) -> bytes:
    records = [
        (tape_ltfs.VTL_NUM_PARTITIONS, bytes([partitions])),
        (tape_ltfs.VTL_MAX_PARTITIONS, bytes([4])),
        (tape_ltfs.VTL_COHERENCY_P1, be(1 if coherency_p1 else 0, 70)),
        (tape_ltfs.VTL_DESCRIPTION, b'Ultrium 7/32T'.ljust(32, b'\0')),
    ]
    for index, capacity in enumerate(capacities):
        records.append((tape_ltfs.VTL_PARTITION_CAPACITY[index], be(capacity)))
    return tlv(records)


def mam_blob(application=b'', coherency=False, fmt=b'') -> bytes:
    return tlv([
        (tape_ltfs.MAM_APPLICATION_VENDOR, b'vtl-1.8 '),
        (tape_ltfs.MAM_APPLICATION_NAME, application.ljust(32, b'\0')),
        (tape_ltfs.MAM_APPLICATION_FORMAT_VERSION, fmt.ljust(16, b'\0')),
        (tape_ltfs.MAM_VOLUME_COHERENCY, be(1 if coherency else 0, 70)),
    ], header=8)


class CartridgeStateTests(TestCase):
    """The rule: two partitions AND a MAM trace. Never one alone."""

    def test_a_blank_cartridge_is_plain(self):
        state = tape_ltfs.state_from_files(
            'I60005L7', vtl_blob(partitions=2), mam_blob(), partitions=1)
        self.assertEqual(state.state, tape_ltfs.PLAIN)
        self.assertFalse(state.was_ltfs)
        self.assertEqual(state.summary, 'not LTFS')

    def test_the_stored_partition_count_is_not_believed(self):
        """A fresh cartridge stores 2 and has one data file. The files win."""
        state = tape_ltfs.state_from_files(
            'I60005L7', vtl_blob(partitions=2), mam_blob(), partitions=1)
        self.assertEqual(state.stored_partition_count, 2)
        self.assertEqual(state.partitions, 1)
        self.assertFalse(state.partitioned)

    def test_an_ltfs_cartridge_is_detected(self):
        state = tape_ltfs.state_from_files(
            'I60001L7',
            vtl_blob(partitions=2, capacities=(262144000, 262144000),
                     coherency_p1=True),
            mam_blob(application=b'LTFS', coherency=True, fmt=b'2.4.0'),
            partitions=2)
        self.assertEqual(state.state, tape_ltfs.LTFS)
        self.assertTrue(state.coherency_present)
        self.assertEqual(state.format_version, '2.4.0')
        self.assertIn('LTFS 2.4.0', state.summary)

    def test_a_wiped_cartridge_is_plain_and_says_why(self):
        """mkltfs --wipe unpartitions and leaves every MAM attribute behind.

        The MAM alone would report a cartridge that cannot be mounted, which is
        wrong in the way that matters: an operator would try.
        """
        state = tape_ltfs.state_from_files(
            'I60003L7',
            vtl_blob(partitions=1, capacities=(524288000,), coherency_p1=True),
            mam_blob(application=b'LTFS', coherency=True, fmt=b'2.4.0'),
            partitions=1)
        self.assertEqual(state.state, tape_ltfs.PLAIN)
        self.assertTrue(state.was_ltfs)
        self.assertIn('formatted once', state.summary)

    def test_two_partitions_without_a_trace_is_not_ltfs(self):
        """Something else partitioned it. The other half of the same rule."""
        state = tape_ltfs.state_from_files(
            'X', vtl_blob(partitions=2), mam_blob(), partitions=2)
        self.assertEqual(state.state, tape_ltfs.PLAIN)
        self.assertFalse(state.was_ltfs)

    def test_mhvtl_s_own_application_vendor_is_not_a_signal(self):
        """mktape sets APPLICATION VENDOR to 'vtl-1.8' on every cartridge, and
        LTFS overwrites it with 'IBM'. Neither ever says LTFS."""
        state = tape_ltfs.state_from_files(
            'X', vtl_blob(partitions=2), mam_blob(), partitions=2)
        self.assertEqual(state.state, tape_ltfs.PLAIN)

    def test_a_17_layout_cartridge_is_unknown(self):
        state = tape_ltfs.state_from_files('OLD', None, mam_blob(), partitions=1)
        self.assertEqual(state.state, tape_ltfs.UNKNOWN)
        self.assertEqual(state.summary, 'not known')

    def test_a_zero_partition_capacity_reads_as_unknown_not_zero(self):
        state = tape_ltfs.state_from_files(
            'X', vtl_blob(partitions=1, capacities=(0,)), mam_blob(), partitions=1)
        self.assertEqual(state.partition_capacities, [None])

    def test_values_are_read_big_endian(self):
        state = tape_ltfs.state_from_files(
            'X', vtl_blob(partitions=1, capacities=(262144000,)), mam_blob(),
            partitions=1)
        self.assertEqual(state.partition_capacities, [262144000])

    def test_a_torn_file_does_not_raise(self):
        truncated = vtl_blob(partitions=2)[:-9]
        state = tape_ltfs.state_from_files('X', truncated, mam_blob(), partitions=2)
        self.assertIn(state.state, (tape_ltfs.PLAIN, tape_ltfs.LTFS))

    def test_an_empty_file_is_unknown(self):
        self.assertEqual(
            tape_ltfs.state_from_files('X', b'', mam_blob(), 1).state,
            tape_ltfs.UNKNOWN)

    def test_to_dict_is_json_safe(self):
        import json
        json.dumps(tape_ltfs.state_from_files(
            'X', vtl_blob(), mam_blob(), 1).to_dict())

    def test_the_new_fields_come_off_mhvtl_data(self):
        """max_partitions, density_name and media_type: read rather than
        defined and forgotten."""
        state = tape_ltfs.state_from_files(
            'X', vtl_blob(partitions=2), mam_blob(), partitions=2)
        self.assertEqual(state.max_partitions, 4)
        self.assertEqual(state.media_description, 'Ultrium 7/32T')

    def test_the_singular_helper_delegates_to_the_plural_one(self):
        """state() mirrors media.usage(); exercised so it is API and not
        decoration."""
        from unittest import mock
        with mock.patch.object(tape_ltfs.media, 'read_media_files',
                               return_value={}) as read:
            result = tape_ltfs.state('I60005L7', partitions=1)
            read.assert_called_once()
        self.assertEqual(result.barcode, 'I60005L7')
        self.assertEqual(result.state, tape_ltfs.UNKNOWN)


@unittest.skipUnless(HAVE_MEDIA, f'no media directory at {MEDIA_ROOT}')
class RealCartridgeTests(TestCase):
    """Against whatever this host actually has.

    Deliberately weak assertions: this runs wherever the suite runs, and the
    cartridges are whatever somebody left behind. It is here to catch a reader
    that raises, or that calls everything LTFS.
    """

    def test_every_cartridge_parses_and_reports_a_known_state(self):
        from apps.libraries.services.tapes import media
        barcodes_seen = sorted(p.name for p in MEDIA_ROOT.iterdir()
                               if p.is_dir())[:40]
        if not barcodes_seen:
            self.skipTest('no cartridges on this host')
        usage = media.usage_for_all(barcodes_seen, MEDIA_ROOT)
        states = tape_ltfs.state_for_all(
            barcodes_seen, {b: u.partitions for b, u in usage.items()},
            MEDIA_ROOT)
        self.assertEqual(set(states), set(barcodes_seen))
        for barcode, state in states.items():
            with self.subTest(barcode=barcode):
                self.assertIn(state.state,
                              (tape_ltfs.LTFS, tape_ltfs.PLAIN, tape_ltfs.UNKNOWN))
                self.assertTrue(state.summary)
                if state.state == tape_ltfs.LTFS:
                    self.assertGreaterEqual(state.partitions, 2)
