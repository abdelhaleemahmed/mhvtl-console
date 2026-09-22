"""Barcodes, media files and the tape service.

Step 5 of the service-layer refactor. Barcode validation gets the most attention
here because of where a barcode ends up: interpolated into `sudo mktape -m` and
into `sudo rm -rf <media>/<barcode>`. The check before this refactor was
`len(barcode) >= 4`.

mktape is mocked in the service tests - the point is the decisions, not whether
mktape works.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.tapes import TapeService, barcodes, media

FIXTURES = Path(__file__).parent / 'fixtures'

#: A slot that is genuinely empty in library_contents.10. Taken from the fixture
#: rather than assumed: slot 33 looks free and holds F01033L6.
EMPTY_SLOT = 25


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


class BarcodeValidationTests(TestCase):
    """The barcode is the thing that reaches a root command line."""

    def test_accepts_a_normal_barcode(self):
        self.assertEqual(barcodes.validate('E01001L8'), 'E01001L8')

    def test_refuses_path_traversal(self):
        """'../..' was accepted before, and reached sudo rm -rf."""
        for attempt in ('../..', '../../etc', 'E01/../..', '/etc/passwd'):
            with self.subTest(barcode=attempt):
                with self.assertRaises(barcodes.InvalidBarcode):
                    barcodes.validate(attempt)

    def test_refuses_shell_metacharacters(self):
        for attempt in ('E01;rm -rf /', 'E01 001', 'E01`id`', 'E01$(id)', 'E01|cat'):
            with self.subTest(barcode=attempt):
                with self.assertRaises(barcodes.InvalidBarcode):
                    barcodes.validate(attempt)

    def test_refuses_newlines(self):
        """A newline used to add arbitrary lines to library_contents."""
        with self.assertRaises(barcodes.InvalidBarcode):
            barcodes.validate('E01001L8\nSlot 99: EVIL')

    def test_refuses_empty_and_overlong(self):
        with self.assertRaises(barcodes.InvalidBarcode):
            barcodes.validate('')
        with self.assertRaises(barcodes.InvalidBarcode):
            barcodes.validate('A' * 17)

    def test_refuses_lowercase(self):
        """MHVTL writes barcodes uppercase; accepting both invites duplicates."""
        with self.assertRaises(barcodes.InvalidBarcode):
            barcodes.validate('e01001l8')


class BarcodeSeriesTests(TestCase):
    def test_reads_density_from_the_suffix(self):
        self.assertEqual(barcodes.density_for('E01001L8'), 'LTO8')
        self.assertEqual(barcodes.density_for('G03001TA'), 'T10KA')

    def test_knows_the_1_8_densities(self):
        """L9, LA, LH and PA arrived with MHVTL 1.8; the old map stopped at L8."""
        self.assertEqual(barcodes.density_for('X01001L9'), 'LTO9')
        self.assertEqual(barcodes.density_for('X01001LA'), 'LTO10')
        self.assertEqual(barcodes.density_for('X01001PA'), 'LTO10P')

    def test_unknown_suffix_is_unknown_not_lto8(self):
        self.assertIsNone(barcodes.density_for('E01001ZZ'))

    def test_reads_the_media_kind(self):
        self.assertEqual(barcodes.kind('CLN101L8'), 'clean')
        self.assertEqual(barcodes.kind('W01001L8'), 'WORM')
        self.assertEqual(barcodes.kind('E01001L8'), 'data')

    def test_splits_and_rebuilds(self):
        self.assertEqual(barcodes.split('E01001L8'), ('E01', '001', 'L8'))
        self.assertEqual(barcodes.build('E01', 1, 'L8'), 'E01001L8')

    def test_detects_the_prefix_a_library_actually_uses(self):
        """Read from the media, not from a brand table that may disagree."""
        self.assertEqual(
            barcodes.detect_prefix(['CLN101L8', 'Q04001L7', 'Q04002L7']), 'Q04')

    def test_next_number_fills_gaps(self):
        """A deleted tape leaves a gap; use it before extending the series."""
        self.assertEqual(
            barcodes.next_number(['E01001L8', 'E01003L8'], 'E01', 'L8'), 2)

    def test_series_is_validated_before_use(self):
        self.assertEqual(barcodes.series('E01', 'L8', 4, 3),
                         ['E01004L8', 'E01005L8', 'E01006L8'])


class MediaPathTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_resolves_inside_the_media_directory(self):
        self.assertEqual(media.path_for('E01001L8', self.root),
                         self.root.resolve() / 'E01001L8')

    def test_refuses_anything_that_escapes(self):
        for attempt in ('../..', '../../etc', '/etc'):
            with self.subTest(barcode=attempt):
                with self.assertRaises((barcodes.InvalidBarcode,
                                        media.MediaPathRefused)):
                    media.path_for(attempt, self.root)

    def test_delete_refuses_an_invalid_barcode_before_touching_anything(self):
        with self.assertRaises(barcodes.InvalidBarcode):
            media.delete('../..', self.root)


class MediaUsageTests(TestCase):
    """Both media layouts, measured in one pass."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def _make(self, barcode, files):
        directory = self.root / barcode
        directory.mkdir(parents=True)
        for name, size in files.items():
            (directory / name).write_bytes(b'x' * size)

    def _find_output(self) -> str:
        """What `find -printf '%h/%f %s\n'` would print for the temp tree.

        Synthesised rather than spawned: the point of these tests is how the
        output is interpreted, and a real find would need the media directory to
        be readable by whoever runs the suite.
        """
        lines = []
        for path in sorted(self.root.glob('*/*')):
            if path.is_file():
                lines.append(f'{path} {path.stat().st_size}')
        return '\n'.join(lines) + '\n'

    def _mam_archive(self, argv):
        """What `tar -cf - -C root BARCODE/mam ...` produces, built here so
        the reader is tested on a real archive rather than a stand-in."""
        import io
        import tarfile
        root = Path(argv[argv.index('-C') + 1])
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as bundle:
            for name in argv[argv.index('--ignore-failed-read') + 1:]:
                blob = root / name
                if blob.is_file():
                    bundle.add(str(blob), arcname=name)
        return buffer.getvalue()

    def _usage(self, names, *, calls=None):
        def fake_sudo(argv, **kwargs):
            if calls is not None:
                calls.append(argv)
            return CommandResult(argv, 0, self._find_output(), '')

        def fake_sudo_bytes(argv, **kwargs):
            if calls is not None:
                calls.append(argv)
            return True, self._mam_archive([str(a) for a in argv]), ''

        with mock.patch.object(media.shell, 'sudo', side_effect=fake_sudo), \
                mock.patch.object(media.shell, 'sudo_bytes',
                                  side_effect=fake_sudo_bytes):
            return media.usage_for_all(names, self.root)

    @staticmethod
    def _mam(capacity_bytes, remaining_bytes=None):
        """A MAM file as mhvtl writes it: two uint32 versions, then
        type-length-value records with 8-byte big-endian counts
        (usr/vtlcart.c:write_mam, usr/vtllib.c:188)."""
        import struct
        blob = struct.pack('<II', 6, 4)
        for attribute, value in ((media.MAM_REMAINING_CAPACITY,
                                  capacity_bytes if remaining_bytes is None
                                  else remaining_bytes),
                                 (media.MAM_MAX_CAPACITY, capacity_bytes)):
            blob += struct.pack('<HH', attribute, 8) + struct.pack('>Q', value)
        return blob

    def test_reads_the_1_7_layout(self):
        self._make('OLD001L8', {'data': 2048, 'indx': 10, 'meta': 512})
        usage = self._usage(['OLD001L8'])['OLD001L8']
        self.assertTrue(usage.exists)
        self.assertEqual(usage.used_bytes, 2048)
        self.assertEqual(usage.layout, '1.7')

    def test_reads_the_1_8_layout(self):
        """Counting files named exactly 'data' reported 0 for every 1.8 tape."""
        self._make('NEW001L8', {'data.0': 4096, 'indx.0': 10, 'meta.0': 512,
                                'mam': 1071})
        usage = self._usage(['NEW001L8'])['NEW001L8']
        self.assertEqual(usage.used_bytes, 4096)
        self.assertEqual(usage.layout, '1.8')

    def test_sums_partitions(self):
        self._make('PART01L8', {'data.0': 1000, 'data.1': 2000, 'mam': 10})
        self.assertEqual(self._usage(['PART01L8'])['PART01L8'].used_bytes, 3000)

    def test_capacity_is_unknown_rather_than_zero(self):
        """0 reads as 'blank tape'; None reads as 'not measured'. A MAM that
        is not one - here ten bytes of nothing - says nothing."""
        self._make('NEW002L8', {'data.0': 100, 'mam': 10})
        usage = self._usage(['NEW002L8'])['NEW002L8']
        self.assertIsNone(usage.capacity_mb)
        self.assertIsNone(usage.used_percent)

    def test_capacity_comes_from_the_mam(self):
        """Every tape has one, in a slot or in a drive, which is what
        `vtlcmd stats` cannot answer for."""
        self._make('CAP001L8', {'data.0': 120 * 1024 * 1024})
        (self.root / 'CAP001L8' / 'mam').write_bytes(
            self._mam(480 * 1024 * 1024, remaining_bytes=360 * 1024 * 1024))
        usage = self._usage(['CAP001L8'])['CAP001L8']
        self.assertEqual(usage.capacity_mb, 480)
        self.assertEqual(usage.used_mb, 120)
        self.assertEqual(usage.used_percent, 25.0)
        self.assertEqual(usage.remaining_bytes, 360 * 1024 * 1024)

    def test_a_mam_that_says_nothing_useful_is_not_a_number(self):
        import struct
        for blob, why in (
                (b'', 'empty'),
                (struct.pack('<II', 6, 4), 'versions only'),
                (self._mam(0), 'a capacity of zero'),
                (self._mam(480 * 1024 * 1024)[:-3], 'a torn last record')):
            with self.subTest(why=why):
                capacity, _ = media.capacity_from_mam(blob)
                self.assertIsNone(capacity)

    def test_a_remaining_larger_than_the_tape_is_ignored(self):
        """A MAM written before anything was recorded can say so."""
        _, remaining = media.capacity_from_mam(
            self._mam(100, remaining_bytes=999))
        self.assertIsNone(remaining)

    def test_missing_media_is_reported_as_missing(self):
        usage = self._usage(['GONE01L8'])['GONE01L8']
        self.assertFalse(usage.exists)

    def test_measures_many_tapes_in_one_pass(self):
        for index in range(5):
            self._make(f'E0100{index}L8', {'data.0': 100 * index, 'mam': 10})
        names = [f'E0100{i}L8' for i in range(5)]

        calls = []
        usage = self._usage(names, calls=calls)
        self.assertEqual(usage['E01004L8'].used_bytes, 400)

        # What matters is that the cost does not grow with the number of
        # tapes: per-tape reads cost about 69ms each, so a 32-tape library
        # spent over four seconds shelling out on every page load.
        for index in range(5, 40):
            self._make(f'E010{index:02d}L8', {'data.0': 100, 'mam': 10})
        many = []
        self._usage([f'E010{i:02d}L8' for i in range(40)], calls=many)
        self.assertEqual(len(many), len(calls),
                         'forty tapes must cost what five did')


class TapeServiceTests(TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10', 'library_contents.20',
                     'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.media = Path(tempfile.mkdtemp())
        self.service = TapeService(self.config, self.media)

    def contents(self, library_id=10):
        return (self.config / f'library_contents.{library_id}').read_text()

    def test_lists_the_tapes_in_a_library(self):
        result = self.service.list(10, with_usage=False)
        self.assertTrue(result.success)
        self.assertEqual(result.data['count'], 32)
        self.assertEqual(result.data['tapes'][0]['barcode'], 'E01001L8')

    def test_list_reports_kind_and_density(self):
        tapes = self.service.list(30, with_usage=False).data['tapes']
        cleaning = [t for t in tapes if t['kind'] == 'clean']
        self.assertTrue(cleaning, 'library 30 holds a cleaning cartridge')

    def test_next_barcode_continues_the_existing_series(self):
        result = self.service.next_barcode(10)
        self.assertTrue(result.success)
        self.assertEqual(result.data['prefix'], 'E01')
        self.assertEqual(result.data['suffix'], 'L8')

    def test_create_refuses_an_invalid_barcode_before_running_mktape(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create(10, '../../etc')
        self.assertFalse(result.success)
        mktape.assert_not_called()

    def test_create_refuses_a_duplicate_barcode(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create(10, 'E01001L8')
        self.assertFalse(result.success)
        self.assertIn('already in library', result.message)
        mktape.assert_not_called()

    def test_create_refuses_an_occupied_slot(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create(10, 'E01099L8', slot=1)
        self.assertFalse(result.success)
        self.assertIn('already holds', result.message)
        mktape.assert_not_called()

    def test_create_records_the_tape_in_library_contents(self):
        with mock.patch.object(media, 'create', return_value=ok()):
            result = self.service.create(10, 'E01099L8', slot=EMPTY_SLOT)
        self.assertTrue(result.success, result.message)
        self.assertIn(f'Slot {EMPTY_SLOT}: E01099L8', self.contents())

    def test_create_makes_the_media_before_recording_it(self):
        """A file with no slot is visible; a slot with no file is a read error."""
        order = []
        with mock.patch.object(media, 'create',
                               side_effect=lambda *a, **k: order.append('media') or ok()):
            with mock.patch.object(TapeService, '_record',
                                   side_effect=lambda *a, **k: order.append('record')
                                   or __import__('apps.libraries.services.core',
                                                 fromlist=['success_result'])
                                   .success_result('ok')):
                self.service.create(10, 'E01099L8', slot=EMPTY_SLOT)
        self.assertEqual(order, ['media', 'record'])

    def test_failed_mktape_leaves_the_slot_empty(self):
        with mock.patch.object(media, 'create',
                               return_value=CommandResult([], 1, '', 'no space left')):
            result = self.service.create(10, 'E01099L8', slot=EMPTY_SLOT)
        self.assertFalse(result.success)
        self.assertIn('no space left', ' '.join(result.errors))
        self.assertNotIn('E01099L8', self.contents())

    def test_bulk_creates_a_numbered_run(self):
        with mock.patch.object(media, 'create', return_value=ok()):
            result = self.service.create_bulk(10, 3, start_number=90)
        self.assertTrue(result.success, result.message)
        for barcode in ('E01090L8', 'E01091L8', 'E01092L8'):
            self.assertIn(barcode, self.contents())

    def test_bulk_stops_at_the_first_failure(self):
        """Better than carrying on and leaving the operator to work out which exist."""
        results = [ok(), CommandResult([], 1, '', 'mktape failed')]
        with mock.patch.object(media, 'create', side_effect=results):
            result = self.service.create_bulk(10, 5, start_number=90)
        self.assertFalse(result.success)
        self.assertIn('E01090L8', self.contents())
        self.assertNotIn('E01092L8', self.contents())

    def test_delete_frees_the_slot(self):
        result = self.service.delete(10, 'E01001L8')
        self.assertTrue(result.success, result.message)
        self.assertNotIn('E01001L8', self.contents())
        self.assertIn('Slot 1:', self.contents())

    def test_delete_leaves_media_alone_unless_asked(self):
        with mock.patch.object(media, 'delete') as remove:
            self.service.delete(10, 'E01001L8')
        remove.assert_not_called()

    def test_delete_removes_media_when_asked(self):
        with mock.patch.object(media, 'delete', return_value=ok()) as remove:
            result = self.service.delete(10, 'E01002L8', remove_media=True)
        remove.assert_called_once()
        self.assertTrue(result.data['media_removed'])

    def test_delete_refuses_an_unknown_barcode(self):
        result = self.service.delete(10, 'NOSUCH01')
        self.assertFalse(result.success)
        self.assertIn('not in library', result.message)

    def test_unreadable_library_is_reported(self):
        service = TapeService(Path(tempfile.mkdtemp()), self.media)
        self.assertFalse(service.list(10).success)


class LibraryMediaTests(TestCase):
    """Tapes are made only in a density the library's drives load.

    Library 10 in the fixture has two ULT3580-TD8 and two ULT3580-TD6 drives;
    library 30 has T10000B drives.
    """

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10', 'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.service = TapeService(self.config, Path(tempfile.mkdtemp()))

    def test_what_library_10_takes(self):
        info = self.service.media_for_library(10)
        self.assertTrue(info['known'])
        self.assertEqual([m['density'] for m in info['media']],
                         ['LTO8', 'LTO7', 'LTO6', 'LTO5', 'LTO4'])
        lto4 = info['media'][-1]
        self.assertFalse(lto4['writable'])
        self.assertEqual(lto4['read_only_in'], ['ULT3580-TD6'])
        self.assertEqual(info['default'], 'LTO8')

    def test_what_library_30_takes(self):
        info = self.service.media_for_library(30)
        self.assertEqual([m['density'] for m in info['media']], ['T10KB', 'T10KA'])
        self.assertEqual(info['default'], 'T10KB')

    def test_a_density_no_drive_loads_is_refused_before_mktape(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create(10, 'E01099TA', slot=EMPTY_SLOT)
        self.assertFalse(result.success)
        self.assertIn('No drive in library 10 loads T10KA', result.message)
        mktape.assert_not_called()

    def test_a_suffix_that_names_another_density_is_refused(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create(10, 'E01099L8', slot=EMPTY_SLOT,
                                         density='LTO6')
        self.assertFalse(result.success)
        self.assertIn('use suffix L6', result.message)
        mktape.assert_not_called()

    def test_a_name_mktape_refuses_is_refused(self):
        result = self.service.create(30, 'K03099TA', density='T10000A')
        self.assertFalse(result.success)
        self.assertIn('not a density', result.message)

    def test_with_no_suffix_the_library_default_is_used(self):
        from apps.libraries.services.core import success_result

        # Library 30 is full in the fixture; the slot is beside the point here.
        with mock.patch.object(media, 'create', return_value=ok()) as mktape, \
                mock.patch.object(TapeService, '_choose_slot', return_value=40), \
                mock.patch.object(TapeService, '_record',
                                  return_value=success_result('recorded')):
            result = self.service.create(30, 'K0309999')
        self.assertTrue(result.success, result.message)
        self.assertEqual(mktape.call_args.kwargs['density'], 'T10KB')

    def test_a_read_only_density_can_still_be_made(self):
        """A restore library is a real thing to build."""
        with mock.patch.object(media, 'create', return_value=ok()):
            result = self.service.create(10, 'E01099L4', slot=EMPTY_SLOT)
        self.assertTrue(result.success, result.message)

    def test_a_library_with_unknown_drives_is_not_refused(self):
        (self.config / 'device.conf').unlink()
        with mock.patch.object(media, 'create', return_value=ok()):
            result = self.service.create(10, 'E01099TA', slot=EMPTY_SLOT)
        self.assertTrue(result.success, result.message)

    def test_bulk_takes_its_suffix_from_the_density(self):
        with mock.patch.object(media, 'create', return_value=ok()) as mktape:
            result = self.service.create_bulk(10, 2, prefix='E01', start_number=90,
                                              density='LTO6')
        self.assertTrue(result.success, result.message)
        self.assertEqual([c['barcode'] for c in result.data['created']],
                         ['E01090L6', 'E01091L6'])
        self.assertEqual(mktape.call_args.kwargs['density'], 'LTO6')

    def test_a_worm_run_uses_the_worm_format_and_suffix(self):
        """W{brand}{lib}{nn}{suffix}: the format the page previews."""
        with mock.patch.object(media, 'create', return_value=ok()) as mktape:
            result = self.service.create_bulk(10, 2, prefix='E01',
                                              density='LTO8', kind='WORM')
        self.assertTrue(result.success, result.message)
        self.assertEqual([c['barcode'] for c in result.data['created']],
                         ['WE1001LY', 'WE1002LY'])
        self.assertEqual(mktape.call_args.kwargs['kind'], 'WORM')

    def test_a_cleaning_run_uses_cln_barcodes(self):
        """The step 8 port made "cleaning" tapes with data barcodes, which
        MHVTL does not treat as cleaning cartridges. CLN101L8 and CLN102L6
        are taken - whatever their suffix - so the run starts at 3."""
        with mock.patch.object(media, 'create', return_value=ok()):
            result = self.service.create_bulk(10, 2, density='LTO6', kind='clean')
        self.assertTrue(result.success, result.message)
        self.assertEqual([c['barcode'] for c in result.data['created']],
                         ['CLN103L6', 'CLN104L6'])
        self.assertTrue(all(c['kind'] == 'clean' for c in result.data['created']))

    def test_a_cleaning_run_cannot_pass_nine(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create_bulk(10, 10, density='LTO8', kind='clean')
        self.assertFalse(result.success)
        self.assertIn('stop at 9', ' '.join(result.errors))
        mktape.assert_not_called()

    def test_bulk_refuses_before_making_anything(self):
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.create_bulk(10, 5, prefix='E01', start_number=90,
                                              density='T10KC')
        self.assertFalse(result.success)
        self.assertIn('No drive', result.message)
        mktape.assert_not_called()

    def test_the_bulk_page_reaches_the_service(self):
        """The bulk page, posted as a browser does, creates the run it asked
        for. It used to go through an adapter that passed kind= to a
        create_bulk() that did not take it and dropped the density, so the page
        raised TypeError."""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import tape_operations_views as views

        request = RequestFactory().post('/', {
            'library_id': '10', 'count': '2', 'barcode_prefix': 'E01',
            'barcode_suffix': '', 'size_mb': '1000', 'density': 'LTO7',
            'tape_type': 'data'})
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        with mock.patch.object(views, '_tapes', return_value=self.service), \
                mock.patch.object(media, 'create', return_value=ok()) as mktape:
            response = views.CreateTapesBulkView.as_view()(request)
        self.assertEqual(response.status_code, 302)
        self.assertIn('tapes', response.url)
        self.assertEqual([c.args[0] for c in mktape.call_args_list],
                         ['E01021L7', 'E01022L7'])   # first free numbers
        self.assertEqual({c.kwargs['density'] for c in mktape.call_args_list}, {'LTO7'})


class SharedNumberSpaceTests(TestCase):
    """A barcode number belongs to a library, not to a tape generation.

    Taken from tape_operations_service.get_next_available_barcode when it moved
    in step 8: it matched by prefix alone, and services was matching prefix and
    suffix. The prefix-only rule is the right one - a library holding E01001L7
    must not be handed E01001L8, because that is a second tape with the same
    number and an operator reading barcodes off a shelf cannot tell them apart.
    """

    def test_a_number_used_by_another_generation_is_taken(self):
        self.assertEqual(barcodes.next_number(['E01001L7', 'E01002L8'], 'E01'), 3)

    def test_a_different_prefix_does_not_reserve_the_number(self):
        self.assertEqual(barcodes.next_number(['M40001L8'], 'E01'), 1)

    def test_gaps_are_filled_across_generations(self):
        self.assertEqual(barcodes.next_number(['E01001L7', 'E01003L8'], 'E01'), 2)

    def test_one_generation_can_still_be_asked_about(self):
        self.assertEqual(
            barcodes.used_numbers(['E01001L7', 'E01002L8'], 'E01', suffix='L8'),
            {2})

    def test_cleaning_tapes_do_not_take_data_numbers(self):
        """CLN101L8 splits to prefix CLN, so it never collides with E01."""
        self.assertEqual(barcodes.next_number(['CLN101L8'], 'E01'), 1)


class ConsecutiveRunTests(TestCase):
    """What a bulk create needs before offering to make N tapes."""

    def test_counts_a_free_run(self):
        self.assertEqual(
            barcodes.consecutive_free(['E01001L8', 'E01005L8'], 'E01', 2), 3)

    def test_a_run_that_starts_on_a_used_number_is_zero(self):
        self.assertEqual(barcodes.consecutive_free(['E01002L8'], 'E01', 2), 0)

    def test_an_empty_library_is_capped_at_the_limit(self):
        """999 means "999 or more", which is enough for any real library."""
        self.assertEqual(barcodes.consecutive_free([], 'E01', 1), 999)

    def test_the_cap_is_adjustable(self):
        self.assertEqual(barcodes.consecutive_free([], 'E01', 1, limit=10), 10)


class CreateMissingTests(TestCase):
    """Media files for barcodes library_contents lists but disk does not hold."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.media = Path(tempfile.mkdtemp())
        self.service = TapeService(self.config, self.media)

    def test_only_missing_tapes_are_made_with_their_own_density(self):
        (self.media / 'E01001L8').mkdir()
        with mock.patch.object(media, 'create', return_value=ok()) as mktape:
            result = self.service.create_missing(10, size_mb=10)
        self.assertTrue(result.success, result.message)
        self.assertIn('E01001L8', result.data['skipped'])
        self.assertEqual(len(result.data['created']), 31)
        by_barcode = {c.args[0]: c.kwargs for c in mktape.call_args_list}
        self.assertEqual(by_barcode['F01030L6']['density'], 'LTO6')
        self.assertEqual(by_barcode['CLN102L6']['kind'], 'clean')
        self.assertEqual(by_barcode['E01002L8']['base'], self.media)

    def test_a_failure_is_reported_with_the_barcode(self):
        with mock.patch.object(media, 'create',
                               return_value=CommandResult([], 1, '', 'disk full')):
            result = self.service.create_missing(10)
        self.assertFalse(result.success)
        self.assertEqual(len(result.data['failed']), 32)


class CreatePageTests(TestCase):
    """The create pages offer each library only the media its drives load.

    They used to offer a fixed list (LTO-1 to LTO-8, SDLT600, DLT4) whatever
    the library held. The pages' behaviour was checked in a browser DOM
    (jsdom) when this changed; these keep the server side and the wiring.
    """

    def _render(self, view_class):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import tape_operations_views as views

        service = TapeService(FIXTURES, tempfile.mkdtemp())

        request = RequestFactory().get('/')
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        libraries = [{'library_id': 10, 'vendor': 'STK', 'product': 'L700', 'brand': None},
                     {'library_id': 30, 'vendor': 'STK', 'product': 'L80', 'brand': None}]
        with mock.patch.object(views, 'get_live_libraries', return_value=libraries), \
                mock.patch.object(views, '_tapes', return_value=service):
            response = view_class.as_view()(request)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_both_pages_carry_each_librarys_media(self):
        import json
        import re

        from apps.libraries import tape_operations_views as views

        for view_class in (views.CreateTapeView, views.CreateTapesBulkView):
            with self.subTest(page=view_class.__name__):
                page = self._render(view_class)
                blob = re.search(r'<script id="media-info" type="application/json">(.*?)</script>',
                                 page, re.S).group(1)
                info = json.loads(blob)
                self.assertEqual([m['density'] for m in info['libraries']['30']['media']],
                                 ['T10KB', 'T10KA'])
                self.assertEqual(info['suffix']['E06'], 'JC')
                self.assertEqual(info['worm_suffix']['LTO8'], 'LY')
                self.assertIn('tape-media.js', page)

    def test_no_page_carries_its_own_tables(self):
        from django.template.loader import get_template

        for name in ('create_tape', 'create_tapes_bulk'):
            with self.subTest(page=name):
                source = get_template(f'libraries/operator/{name}.html').template.source
                self.assertNotIn('wormSuffixes', source)
                self.assertNotIn('densityToSuffix', source)
                self.assertNotIn("value == 'LTO8'", source)


class AdoptTests(TestCase):
    """Giving a tape that still has its files back to a library.

    `tape delete` without --remove-media leaves the data on disk with no
    library listing it. Adopting writes the barcode into a slot and nothing
    else: the tape comes back with everything that was on it.
    """

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10', 'library_contents.20',
                     'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.media = Path(tempfile.mkdtemp())
        self.service = TapeService(self.config, self.media)
        # An orphan on disk: files, but in no library_contents.
        self.orphan = self.media / 'E01099L8'
        self.orphan.mkdir()
        (self.orphan / 'data.0').write_bytes(b'')

    def free_a_slot(self, library_id=10):
        result = self.service.delete(library_id, self.service.list(
            library_id, with_usage=False).data['tapes'][0]['barcode'])
        self.assertTrue(result.success, result.message)

    def test_it_puts_the_barcode_in_a_free_slot(self):
        self.free_a_slot()
        with mock.patch.object(media, 'create') as mktape:
            result = self.service.adopt(10, 'E01099L8')
        self.assertTrue(result.success, result.message)
        mktape.assert_not_called()
        self.assertIn('E01099L8', self.service.barcodes_in(10))

    def test_the_media_is_never_touched(self):
        self.free_a_slot()
        with mock.patch.object(media, 'delete') as remove:
            self.service.adopt(10, 'E01099L8')
        remove.assert_not_called()
        self.assertTrue((self.orphan / 'data.0').exists())

    def test_it_refuses_a_tape_with_no_files(self):
        self.free_a_slot()
        result = self.service.adopt(10, 'E01098L8')
        self.assertFalse(result.success)
        self.assertIn('no media files', result.message)
        self.assertIn('tape create', ' '.join(result.errors))

    def test_it_refuses_a_tape_another_library_already_has(self):
        """Two libraries listing one tape means two robots moving one set of
        data."""
        owned = self.service.list(20, with_usage=False).data['tapes'][0]['barcode']
        (self.media / owned).mkdir()
        self.free_a_slot()
        result = self.service.adopt(10, owned)
        self.assertFalse(result.success)
        self.assertIn('already in library 20', result.message)

    def test_it_refuses_a_density_the_drives_do_not_load(self):
        self.free_a_slot()
        (self.media / 'E01097L1').mkdir()
        result = self.service.adopt(10, 'E01097L1')
        self.assertFalse(result.success)
        self.assertIn('LTO1', result.message)

    def test_a_full_library_is_refused_before_anything_is_written(self):
        """Library 30's forty slots all hold a tape."""
        (self.media / 'G03099TA').mkdir()
        result = self.service.adopt(30, 'G03099TA')
        self.assertFalse(result.success)
        self.assertIn('no empty slots', result.message)
        self.assertNotIn('G03099TA', self.service.barcodes_in(30))

    def test_the_slot_can_be_chosen(self):
        """Slot 24 of library 10 is empty in the fixture."""
        result = self.service.adopt(10, 'E01099L8', slot=24)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['slot'], 24)

    def test_an_occupied_slot_is_refused(self):
        result = self.service.adopt(10, 'E01099L8', slot=1)
        self.assertFalse(result.success)
        self.assertIn('already holds', result.message)

    def test_it_says_a_restart_is_needed(self):
        self.free_a_slot()
        result = self.service.adopt(10, 'E01099L8')
        self.assertTrue(result.data['restart_required'])

    def test_owner_of_finds_the_library_that_lists_a_barcode(self):
        owned = self.service.list(30, with_usage=False).data['tapes'][0]['barcode']
        self.assertEqual(self.service.owner_of(owned), 30)
        self.assertIsNone(self.service.owner_of('E01099L8'))
