"""Characterisation tests for the four parsers, against captured real output.

Step 0 of the service-layer refactor (docs/sphinx/guides/refactoring.rst). These
parsers - device.conf, library_contents, mtx status, lsscsi - are the pieces the
CLI will lean on hardest, and they had no test coverage at all. They have now
moved into services/config/, services/operations/ and services/scsi/, and
these tests moved with them: the assertions are unchanged, so they still say the
new parser answers what the old one did on the same real output. That is what a
characterisation test is for, and rewriting the assertions to match the new code
would have thrown it away.

The fixtures in tests/fixtures/ were captured from a working MHVTL 1.8 host with
three libraries (STK L700, SONY LIB-302, STK L80), twelve drives and 97 tapes.
They are real output, not hand-written: hand-written samples agree with whatever
the parser already does, which is exactly the bug these tests exist to catch.
"""
from pathlib import Path
from types import SimpleNamespace

from django.test import TestCase

from apps.libraries.services.config import device_conf
from apps.libraries.services.operations import mtx
from apps.libraries.services.scsi import lsscsi
from apps.libraries.services.tapes import TapeService

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return (FIXTURES / name).read_text()


class DeviceConfParserTests(TestCase):
    """services/config/device_conf.parse - the best of four rival
    implementations, and the one that survived the refactor.

    Pinned first against mhvtl_library_service._parse_device_conf_rpm, which it
    replaced in step 8; the dict access below is device_conf.parse's to_dict(),
    so the same assertions hold.
    """

    @classmethod
    def setUpTestData(cls):
        cls.parsed = device_conf.parse(fixture('device.conf')).to_dict()

    def test_finds_every_library(self):
        # libraries and drives come back as dicts keyed by id, not lists.
        self.assertEqual(sorted(self.parsed['libraries']), [10, 20, 30])

    def test_reads_vendor_and_product(self):
        libraries = self.parsed['libraries']
        self.assertEqual(libraries[10]['vendor'], 'STK')
        self.assertEqual(libraries[10]['product'], 'L700')
        self.assertEqual(libraries[20]['vendor'], 'SONY')
        self.assertEqual(libraries[20]['product'], 'LIB-302')

    def test_reads_the_scsi_address(self):
        """What device mapping matches on, so it has to survive the move."""
        library = self.parsed['libraries'][20]
        self.assertEqual((library['channel'], library['target'], library['lun']),
                         (0, 13, 0))

    def test_finds_every_drive(self):
        self.assertEqual(sorted(self.parsed['drives']), list(range(11, 15))
                         + list(range(21, 25)) + list(range(31, 35)))

    def test_drives_know_their_library(self):
        self.assertEqual(self.parsed['drives'][21]['library_id'], 20)

    def test_counts_drives_per_library(self):
        counts = self.parsed.get('drive_counts') or {}
        self.assertEqual({int(k): v for k, v in counts.items()}, {10: 4, 20: 4, 30: 4})

    def test_libraries_keep_config_file_order(self):
        """Order is 10, 30, 20 here - not sorted, and not SCSI order either.

        Worth pinning: matching this list positionally against lsscsi output is
        the mistake that sends an operation to the wrong library.
        """
        self.assertEqual(list(self.parsed['libraries']), [10, 30, 20])

    def test_parsing_is_pure(self):
        """Takes text, not a path: no file I/O and no sudo, so it is testable."""
        self.assertEqual(device_conf.parse(fixture('device.conf')).to_dict(),
                         self.parsed)


class LibraryContentsParserTests(TestCase):
    """The tapes a library_contents file holds, through TapeService.list.

    These went through the old adapter's _parse_library_contents until it was
    removed; the assertions are the originals, on the same real fixtures.
    """

    def tapes(self, library_id):
        result = TapeService(FIXTURES).list(library_id, with_usage=False)
        return ([SimpleNamespace(**tape) for tape in result.data['tapes']]
                if result.success else [])

    def test_reads_every_occupied_slot(self):
        tapes = self.tapes(10)
        self.assertEqual(len(tapes), 32)

    def test_slot_and_barcode_pairs(self):
        by_slot = {t.slot: t.barcode for t in self.tapes(10)}
        self.assertEqual(by_slot[1], 'E01001L8')
        self.assertEqual(by_slot[2], 'E01002L8')

    def test_empty_slots_are_not_reported_as_tapes(self):
        """library_contents.10 declares more slots than it has tapes."""
        tapes = self.tapes(10)
        self.assertTrue(all(t.barcode for t in tapes))

    def test_each_library_is_read_separately(self):
        self.assertEqual(len(self.tapes(20)), 25)
        self.assertEqual(len(self.tapes(30)), 40)

    def test_cleaning_tapes_are_present(self):
        barcodes = [t.barcode for t in self.tapes(30)]
        self.assertTrue(any(b.startswith('CLN') for b in barcodes),
                        'library 30 holds cleaning cartridges')

    def test_missing_library_yields_nothing(self):
        self.assertEqual(self.tapes(99), [])


class MtxStatusParserTests(TestCase):
    """services/operations/mtx.parse.

    Pinned first against tape_operations_service._parse_mtx_status, which it
    replaced in step 8. The helpers below put its answers in the shape the old
    result carried - dicts with slot_num/drive_num keys - so the assertions are
    the ones originally written.
    """

    def parse(self, name, library_id=10):
        return mtx.parse(fixture(name))

    def summary(self, name, library_id=10):
        return self.parse(name, library_id).summary

    def drives(self, name, library_id=10):
        return {drive.number: drive.to_dict()
                for drive in self.parse(name, library_id).drives}

    def storage_slots(self, name, library_id=10):
        return [slot.to_dict() for slot in self.parse(name, library_id).slots]

    def test_counts_drives_and_slots(self):
        summary = self.summary('mtx-status-lib10.txt')
        self.assertEqual(summary['total_drives'], 4)
        self.assertEqual(summary['total_slots'], 39)
        self.assertEqual(summary['ie_slots'], 4)

    def test_reads_a_loaded_drive(self):
        """Drive 0 holds E01003L8, taken from Storage Element 3."""
        drives = self.drives('mtx-status-lib10.txt')
        self.assertTrue(drives[0]['full'])
        self.assertEqual(drives[0]['barcode'], 'E01003L8')
        self.assertEqual(drives[0].get('slot_origin'), 3)

    def test_empty_drives_carry_no_barcode(self):
        drives = self.drives('mtx-status-lib10.txt')
        self.assertFalse(drives[1]['full'])
        self.assertIn(drives[1].get('barcode'), (None, ''))

    def test_full_and_empty_slots_add_up(self):
        summary = self.summary('mtx-status-lib10.txt')
        self.assertEqual(summary['full_slots'] + summary['empty_slots'],
                         summary['total_slots'])

    def test_a_second_library_parses_independently(self):
        summary = self.summary('mtx-status-lib20.txt', library_id=20)
        self.assertEqual(summary['total_drives'], 4)
        self.assertEqual(summary['full_slots'], 25)

    def test_barcodes_are_stripped(self):
        """mtx pads VolumeTag with trailing spaces."""
        for slot in self.storage_slots('mtx-status-lib10.txt'):
            if slot.get('barcode'):
                self.assertEqual(slot['barcode'], slot['barcode'].strip())


class LsscsiParserTests(TestCase):
    """services/scsi/lsscsi.parse.

    Pinned first against tape_operations_service._parse_lsscsi_line, which it
    replaced in step 8. `host` below is the services ScsiAddress rendered the
    way the old string field read, so the same assertions hold.
    """

    def setUp(self):
        self.devices = lsscsi.parse(fixture('lsscsi-g.txt'))

    def test_parses_every_line(self):
        self.assertEqual(len(self.devices), len(
            [l for l in fixture('lsscsi-g.txt').splitlines() if l.strip()]))

    def test_identifies_changers_and_tapes(self):
        kinds = [d.device_type for d in self.devices]
        self.assertEqual(kinds.count('mediumx'), 3)
        self.assertEqual(kinds.count('tape'), 12)

    def test_keeps_the_scsi_address(self):
        """The address is what device mapping matches on, so it must survive."""
        changers = [d for d in self.devices if d.device_type == 'mediumx']
        self.assertTrue(all(d.address is not None for d in changers))
        self.assertIn('[16:0:0:0]', [str(d.address) for d in changers])

    def test_reads_generic_device_paths(self):
        changers = {str(d.address): d.generic_path for d in self.devices
                    if d.device_type == 'mediumx'}
        self.assertEqual(changers['[16:0:0:0]'], '/dev/sg4')

    def test_reads_vendor_and_model(self):
        by_address = {str(d.address): d for d in self.devices}
        self.assertEqual(by_address['[16:0:0:0]'].vendor, 'STK')
        self.assertEqual(by_address['[16:0:0:0]'].model, 'L700')

    def test_a_model_containing_a_space_no_longer_shifts_the_columns(self):
        """This was written as a known weakness and is now an assertion.

        Splitting on whitespace made 'HP Ultrium 6-SCSI' one field too many, so
        every column after it shifted and the device paths came back wrong - the
        parser handed out /dev/st9 as the generic path. services/scsi/lsscsi
        parses by column position, so the test says what it should have.
        """
        line = '[5:0:1:0]    tape    HP       Ultrium 6-SCSI   1.00  /dev/st9   /dev/sg20'
        device = lsscsi.parse_line(line)

        self.assertIsNotNone(device)
        self.assertEqual(device.model, 'Ultrium 6-SCSI')
        self.assertEqual(device.device_path, '/dev/st9')
        self.assertEqual(device.generic_path, '/dev/sg20')
