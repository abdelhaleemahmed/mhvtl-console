"""Drive CRUD in services/drives.

Step 2 of the service-layer refactor. These functions were stranded in
backup_mhvtl_library_service.py, a fork with zero importers, while the views
called them on a service that did not define them - so they have never actually
run. They are moved rather than rewritten, but moved code is not proven code,
which is why this suite exists.

Every test works on a throwaway copy of the captured fixtures, never on
/etc/mhvtl.
"""
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core import success_result
from apps.libraries.services.drives import DriveInfo, DriveService
from apps.libraries.services.profiles import personalities as drive_service_personalities

FIXTURES = Path(__file__).parent / 'fixtures'
CONFIG_FILES = ['device.conf', 'library_contents.10',
                'library_contents.20', 'library_contents.30']


class DriveServiceTestCase(TestCase):
    """Gives each test its own copy of the configuration."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in CONFIG_FILES:
            shutil.copy(FIXTURES / name, self.config)
        self.service = DriveService(self.config)

    def device_conf(self) -> str:
        return (self.config / 'device.conf').read_text()

    def contents(self, library_id: int) -> str:
        return (self.config / f'library_contents.{library_id}').read_text()

    def drive_ids(self, library_id=None):
        return [d['drive_id'] for d in self.service.list(library_id).data['drives']]


class ListingTests(DriveServiceTestCase):
    def test_lists_every_drive(self):
        result = self.service.list()
        self.assertTrue(result.success)
        self.assertEqual(result.data['count'], 12)

    def test_filters_by_library(self):
        self.assertEqual(self.drive_ids(10), [11, 12, 13, 14])
        self.assertEqual(self.drive_ids(20), [21, 22, 23, 24])

    def test_reads_drive_details(self):
        drive = self.service.get(21).data['drive']
        self.assertEqual(drive['library_id'], 20)
        self.assertEqual(drive['product'], 'SDX-900V')
        self.assertEqual((drive['channel'], drive['target'], drive['lun']), (0, 14, 0))

    def test_unknown_drive_is_a_failure_not_an_exception(self):
        result = self.service.get(99)
        self.assertFalse(result.success)
        self.assertIn('not found', result.message)

    def test_unreadable_config_is_reported(self):
        service = DriveService(Path(tempfile.mkdtemp()))
        result = service.list()
        self.assertFalse(result.success)
        self.assertIn('Could not read', result.message)

    def test_empty_library_lists_nothing(self):
        self.assertEqual(self.service.list(99).data['count'], 0)


class AddDriveTests(DriveServiceTestCase):
    def test_adds_a_drive_to_a_library(self):
        result = self.service.add(10)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['drive_id'], 15)
        self.assertEqual(self.drive_ids(10), [11, 12, 13, 14, 15])

    def test_drive_id_follows_library_plus_slot(self):
        """MHVTL's convention: library 10's fifth drive is 15, in slot 5."""
        result = self.service.add(10)
        self.assertEqual(result.data['slot'], 5)
        self.assertEqual(result.data['drive_id'], 10 + result.data['slot'])

    def test_takes_the_lowest_free_scsi_target(self):
        """Reusing a taken target would give two devices the same address."""
        self.assertEqual(self.service.add(10).data['target'], 5)

    def test_two_drives_do_not_collide(self):
        first = self.service.add(10)
        second = self.service.add(10)
        self.assertNotEqual(first.data['drive_id'], second.data['drive_id'])
        self.assertNotEqual(first.data['target'], second.data['target'])

    def test_writes_a_complete_device_conf_entry(self):
        self.service.add(10)
        text = self.device_conf()
        self.assertIn('Drive: 15 CHANNEL: 00 TARGET: 05 LUN: 00', text)
        self.assertIn(' Library ID: 10 Slot: 05', text)
        self.assertIn(' Vendor identification: IBM', text)
        self.assertIn(' Product identification: ULT3580-TD8', text)

    def test_honours_supplied_vendor_and_product(self):
        """A model the library takes - the STK L700's profile lists the LTO
        drives as well as the T9840 and T10000 families."""
        self.service.add(10, {'vendor': 'IBM', 'product': 'ULT3580-TD7',
                              'serial': 'ABC123'})
        text = self.device_conf()
        self.assertIn(' Vendor identification: IBM', text)
        self.assertIn(' Product identification: ULT3580-TD7', text)
        self.assertIn(' Unit serial number: ABC123', text)

    def test_refuses_a_drive_the_library_does_not_take(self):
        """The create form has always applied the profile's cascade - a library
        model offers only the drives listed for it - and adding a drive
        afterwards did not, so a library could end up with a drive its model
        never had. An HP Ultrium is not in the STK L700's list."""
        before = self.device_conf()
        result = self.service.add(10, {'vendor': 'HP', 'product': 'Ultrium 8-SCSI'})
        self.assertFalse(result.success)
        self.assertIn('does not take', result.message)
        self.assertIn('it takes:', ' '.join(result.errors))
        self.assertEqual(self.device_conf(), before)

    def test_allows_the_model_the_library_already_holds(self):
        """Libraries on real hosts do not always match their profile; refusing
        a drive identical to the ones already in it would help nobody."""
        allowed = self.service._allowed_drives(
            'SONY', 'LIB-302', {1: {'product': 'SDX-900V', 'slot': 1}})
        self.assertIn('SDX-900V', allowed)

    def test_the_placement_is_what_add_would_do(self):
        """The page shows this before the operator commits, so it has to be
        what add() then does."""
        plan = self.service.placement(10).data
        self.assertEqual(plan['slot'], 5)          # four drives in the fixture
        self.assertEqual(plan['serial'], 'XYZZY_AD5')
        self.assertIn('ULT3580-TD8', plan['supported'])
        self.assertIsNotNone(plan['drive_id'])
        self.assertIsNotNone(plan['target'])

        self.service.add(10, {})
        text = self.device_conf()
        self.assertIn('Drive: %s ' % plan['drive_id'], text)
        self.assertIn('Library ID: 10 Slot: 0%s' % plan['slot'], text)
        self.assertIn('TARGET: %02d' % plan['target'], text)

    def test_the_placement_says_when_a_library_is_full(self):
        # max_drives is a property of the layout, so this stands in for one.
        small = mock.Mock(max_drives=4, title='a four-drive layout',
                          source='the test')
        with mock.patch.object(drive_service_personalities, 'library_layout',
                               return_value=small):
            result = self.service.placement(10)
        self.assertFalse(result.success)
        self.assertIn('full', result.message)
        self.assertTrue(result.data['full'])
        self.assertEqual(result.data['drives_now'], 4)

    def test_refuses_a_model_mhvtl_does_not_recognise(self):
        """This test used to add an 'Ultrium 9-SCSI', which is not in vtltape's
        table - HP goes to 8 - so MHVTL would have emulated a generic drive."""
        before = self.device_conf()
        result = self.service.add(10, {'vendor': 'HP', 'product': 'Ultrium 9-SCSI'})
        self.assertFalse(result.success)
        self.assertIn('generic drive', ' '.join(result.errors))
        self.assertEqual(self.device_conf(), before)

    def test_a_new_drive_matches_the_librarys_existing_drives(self):
        result = self.service.add(30)          # library 30 holds T10000B drives
        self.assertTrue(result.success, result.errors)
        entry = self.device_conf().split(f"Drive: {result.data['drive_id']} ")[1]
        self.assertIn(' Product identification: T10000B', entry.split('\n\n')[0])

    def test_a_tenth_drive_does_not_take_the_next_librarys_id(self):
        """library_id + slot would give library 10's tenth drive id 20."""
        ids_given = []
        for _ in range(6):                     # the fixture has 4; this makes 10
            result = self.service.add(10)
            self.assertTrue(result.success, result.errors)
            ids_given.append(result.data['drive_id'])
        self.assertEqual(ids_given[:5], [15, 16, 17, 18, 19])
        self.assertNotIn(20, ids_given)
        self.assertNotIn(ids_given[-1] % 10, [0])
        self.assertEqual(ids_given[-1], 25)    # 20 is library 20; 21-24 are its drives

    def test_adds_the_drive_to_library_contents(self):
        self.service.add(10)
        self.assertIn('Drive 5:', self.contents(10))

    def test_new_drive_line_joins_the_existing_drive_block(self):
        """The drive lines are a block at the top; a new one belongs with them."""
        self.service.add(10)
        lines = [l.strip() for l in self.contents(10).splitlines() if l.strip()]
        drive_lines = [i for i, l in enumerate(lines) if l.startswith('Drive ')]
        self.assertEqual(drive_lines,
                         list(range(drive_lines[0], drive_lines[0] + 5)))

    def test_unknown_library_is_refused(self):
        result = self.service.add(99)
        self.assertFalse(result.success)
        self.assertIn('not found', result.message)
        self.assertNotIn('Drive: 100', self.device_conf())

    def test_a_scratch_directory_never_touches_the_daemons(self):
        """The daemons run from the live configuration; this instance writes
        somewhere else, so they are left alone and a restart is still needed."""
        result = self.service.add(10)
        self.assertTrue(result.data['restart_required'])
        self.assertFalse(result.data['restarted'])
        self.assertIn('not the live configuration directory', result.message)

    def test_existing_drives_are_untouched(self):
        before = self.device_conf()
        self.service.add(10)
        after = self.device_conf()
        for drive in ('Drive: 11', 'Drive: 21', 'Drive: 31'):
            self.assertEqual(before.count(drive), after.count(drive))


class RemoveDriveTests(DriveServiceTestCase):
    def test_removes_a_drive(self):
        result = self.service.remove(14)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.drive_ids(10), [11, 12, 13])

    def test_removes_the_whole_device_conf_block(self):
        self.service.remove(14)
        self.assertNotIn('Drive: 14 CHANNEL:', self.device_conf())

    def test_leaves_neighbouring_drives_intact(self):
        self.service.remove(12)
        self.assertEqual(self.drive_ids(10), [11, 13, 14])
        self.assertIn('Drive: 13 CHANNEL:', self.device_conf())

    def test_removes_the_line_from_library_contents(self):
        self.service.remove(14)
        self.assertNotIn('Drive 4:', self.contents(10))

    def test_unknown_drive_is_refused_without_touching_the_file(self):
        before = self.device_conf()
        result = self.service.remove(99)
        self.assertFalse(result.success)
        self.assertEqual(self.device_conf(), before)

    def test_add_then_remove_restores_the_original_file(self):
        """The strongest check that an entry is written and removed cleanly."""
        before = self.device_conf()
        added = self.service.add(10)
        self.service.remove(added.data['drive_id'])
        self.assertEqual(self.device_conf().strip(), before.strip())


class UpdateDriveTests(DriveServiceTestCase):
    def test_changes_vendor_and_product(self):
        result = self.service.update(11, {'vendor': 'HP', 'product': 'Ultrium 9-SCSI'})
        self.assertTrue(result.success, result.message)
        drive = self.service.get(11).data['drive']
        self.assertEqual(drive['vendor'], 'HP')
        self.assertEqual(drive['product'], 'Ultrium 9-SCSI')

    def test_changes_only_the_named_drive(self):
        self.service.update(11, {'vendor': 'HP'})
        self.assertEqual(self.service.get(12).data['drive']['vendor'], 'IBM')

    def test_keeps_the_scsi_address(self):
        """Identification fields only: the address decides device mapping."""
        before = self.service.get(11).data['drive']
        self.service.update(11, {'vendor': 'HP'})
        after = self.service.get(11).data['drive']
        self.assertEqual((before['channel'], before['target'], before['lun']),
                         (after['channel'], after['target'], after['lun']))

    def test_nothing_to_change_is_refused(self):
        result = self.service.update(11, {'library_id': 30})
        self.assertFalse(result.success)
        self.assertIn('Editable fields', ' '.join(result.errors))

    def test_unknown_drive_is_refused(self):
        self.assertFalse(self.service.update(99, {'vendor': 'HP'}).success)


class DriveInfoTests(TestCase):
    def test_address_is_none_when_incomplete(self):
        self.assertIsNone(DriveInfo(drive_id=11, channel=0, target=1).address)

    def test_address_when_complete(self):
        self.assertEqual(DriveInfo(drive_id=11, channel=0, target=1, lun=0).address,
                         (0, 1, 0))

    def test_unit_name_matches_the_systemd_instance(self):
        self.assertEqual(DriveInfo(drive_id=11).unit_name, 'vtltape@11.service')


class DaemonTests(TestCase):
    """Adding or removing a drive makes the running daemons match device.conf.

    Both used to write device.conf and tell the operator to restart MHVTL by
    hand, so a drive added from the page was in the configuration and invisible
    to the library until someone did.
    """

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        shutil.copy(FIXTURES / 'library_contents.10', self.config)
        self.service = DriveService(self.config)

    def _units(self):
        """The daemons are only touched for the live configuration directory,
        so these tests make the scratch directory look like it."""
        from apps.libraries.services.console import units
        from apps.libraries.services.drives import service as drive_service
        patch = mock.patch.object(drive_service, 'config_dir',
                                  return_value=self.config)
        patch.start()
        self.addCleanup(patch.stop)
        # The restart is mocked, so nothing was re-created: nothing to rebind,
        # and the live exports must never be reached from here.
        rebind = mock.patch.object(drive_service.iscsi_bindings, 'after_restart',
                                   return_value='')
        rebind.start()
        self.addCleanup(rebind.stop)
        return units

    def test_adding_starts_the_drive_and_restarts_the_library(self):
        units = self._units()
        with mock.patch.object(units, 'start_library',
                               return_value={'vtltape@15.service': True}) as started, \
                mock.patch.object(units, 'restart_library',
                                  return_value={'ok': True, 'restarted': 'vtllibrary@10.service',
                                                'error': None}) as restarted:
            result = self.service.add(10)
        self.assertTrue(result.success, result.message)
        self.assertEqual(started.call_args.args[0], 10)
        self.assertEqual(restarted.call_args.args[0], 10)
        self.assertIn('started', result.message)
        self.assertTrue(result.data['restarted'])

    def test_removing_stops_the_drive_and_restarts_the_library(self):
        units = self._units()
        with mock.patch.object(units, 'stop') as stopped, \
                mock.patch.object(units, 'disable'), \
                mock.patch.object(units, 'reset_failed') as cleared, \
                mock.patch.object(units, 'restart_library',
                                  return_value={'ok': True, 'restarted': 'vtllibrary@10.service',
                                                'error': None}):
            result = self.service.remove(14)
        self.assertTrue(result.success, result.message)
        self.assertEqual(stopped.call_args.args[0], 'vtltape@14.service')
        # systemd keeps a stopped instance "failed" until it is reset, and the
        # console then reports a unit for a drive that no longer exists
        self.assertEqual(cleared.call_args.args[0], 'vtltape@14.service')
        self.assertIn('stopped', result.message)

    def test_no_restart_leaves_the_daemons_alone(self):
        units = self._units()
        with mock.patch.object(units, 'start_library') as started, \
                mock.patch.object(units, 'restart_library') as restarted:
            result = self.service.add(10, restart=False)
        self.assertTrue(result.success, result.message)
        started.assert_not_called()
        restarted.assert_not_called()
        self.assertTrue(result.data['restart_required'])

    def test_a_restart_that_fails_is_reported_not_hidden(self):
        units = self._units()
        with mock.patch.object(units, 'start_library',
                               return_value={'vtltape@15.service': True}), \
                mock.patch.object(units, 'restart_library',
                                  return_value={'ok': False, 'restarted': 'vtllibrary@10.service',
                                                'error': 'Job failed'}):
            result = self.service.add(10)
        self.assertTrue(result.success, 'the configuration was still written')
        self.assertIn('did not restart', result.message)
        self.assertFalse(result.data['restarted'])


class ActivityTests(DriveServiceTestCase):
    """What each drive is doing, from `vtlcmd <drive> stats`.

    That command is our own addition to MHVTL (patches/0004). It answers over
    the daemon's message queue, so it keeps reporting during a backup, when
    the kernel holds the SCSI reservation for the initiator and mt and mtx
    only say "device busy". Nothing here talks to a daemon: vtlcmd is faked.

    The service decides what a drive is doing and says it in words, because
    the library page and `mhvtl status activity` must not work it out
    separately and disagree.
    """

    def setUp(self):
        super().setUp()
        from apps.libraries.services.drives import service as drive_service
        drive_service.samples.forget()
        self.module = drive_service

    def _stats(self, **fields):
        from apps.libraries.services.operations.vtlcmd import TapeStats
        return TapeStats(**{'barcode': 'E01001L8', 'loaded': True, **fields})

    def _activity(self, answers, settle=0, library_id=10):
        """answers: drive_id -> TapeStats or None, as the daemon would reply.

        A list per drive is read one entry per call, which is how a drive
        that is moving is described: two readings, the second larger.
        """
        remaining = {key: list(value) if isinstance(value, list) else [value]
                     for key, value in answers.items()}

        def reply(drive_id):
            queue = remaining.get(drive_id)
            if not queue:
                return None
            return queue.pop(0) if len(queue) > 1 else queue[0]

        with mock.patch.object(self.module.vtlcmd, 'stats', side_effect=reply), \
                mock.patch.object(self.module.time, 'sleep'):
            return self.module.DriveService(self.config).activity(
                library_id, settle=settle)

    def test_a_row_for_every_drive_in_the_library(self):
        result = self._activity({})
        self.assertTrue(result.success, result.message)
        self.assertEqual([d['drive_id'] for d in result.data['drives']],
                         [11, 12, 13, 14])
        self.assertEqual(result.data['total'], 4)
        self.assertEqual(result.data['library_id'], 10)

    def test_a_drive_whose_counters_grow_is_writing(self):
        result = self._activity({11: [self._stats(written=28405760),
                                      self._stats(written=169082880)]},
                                settle=1)
        drive = result.data['drives'][0]
        self.assertEqual(drive['state'], 'writing')
        self.assertEqual(drive['label'], 'writing E01001L8')
        self.assertIn('161.2 MB written', drive['detail'])
        self.assertEqual(result.data['busy'], 1)
        self.assertIn('1 working', result.message)

    def test_a_drive_being_read_says_reading(self):
        result = self._activity({11: [self._stats(read=1024),
                                      self._stats(read=41943040)]}, settle=1)
        self.assertEqual(result.data['drives'][0]['state'], 'reading')

    def test_one_reading_is_holding_not_writing(self):
        """With nothing to compare against, saying "writing" would be a guess."""
        result = self._activity({11: self._stats(written=41943040)})
        drive = result.data['drives'][0]
        self.assertEqual(drive['state'], 'holding')
        self.assertEqual(drive['label'], 'holding E01001L8')

    def test_the_previous_reading_is_remembered_for_the_next_question(self):
        """A page polling every few seconds brings its own comparison, so the
        second call knows what the first could not."""
        self._activity({11: self._stats(written=28405760)})
        result = self._activity({11: self._stats(written=169082880)})
        self.assertEqual(result.data['drives'][0]['state'], 'writing')

    def test_a_reading_too_old_to_mean_anything_is_not_used(self):
        """A finished backup must not be called "writing" because of a sample
        from two minutes ago."""
        self._activity({11: self._stats(written=28405760)})
        stale = time.time() - self.module._SAMPLE_LIFE - 1
        # min_age=0: planting a reading deliberately, rather than the
        # ordinary path that refuses to replace one only seconds old.
        self.module.samples.remember(
            {11: self._stats(written=28405760).to_dict()}, now=stale,
            min_age=0)
        result = self._activity({11: self._stats(written=169082880)})
        self.assertEqual(result.data['drives'][0]['state'], 'holding')

    def test_the_sizes_are_words_the_caller_prints_as_they_are(self):
        result = self._activity({11: self._stats(written=41943040, read=2048,
                                                 written_media=41943040,
                                                 capacity=524288000)})
        detail = result.data['drives'][0]['detail']
        self.assertEqual(detail, '40.0 MB written, 2.0 KB read - '
                                 '8.0% of the tape')

    def test_a_ratio_of_one_is_not_worth_a_clause(self):
        """MHVTL reports 1.0 for data that did not compress, which is most of
        a test run."""
        result = self._activity({11: self._stats(written=41943040,
                                                 written_media=41943040)})
        self.assertNotIn('compression', result.data['drives'][0]['detail'])

    def test_real_compression_is_reported(self):
        result = self._activity({11: self._stats(written=41943040,
                                                 written_media=199040)})
        self.assertIn('210.73x compression', result.data['drives'][0]['detail'])

    def test_it_counts_the_drives_holding_a_tape(self):
        result = self._activity({11: self._stats(),
                                 13: self._stats(barcode='E01002L8')})
        self.assertEqual(result.data['loaded'], 2)
        self.assertIn('2 of 4', result.message)

    def test_a_loaded_drive_carries_the_bar_the_panel_draws(self):
        """The panel shows how full the tape is, and must not work out the
        width or the colour itself."""
        result = self._activity({11: self._stats(written=363 * 1024 * 1024,
                                                 written_media=363 * 1024 * 1024,
                                                 capacity=480 * 1024 * 1024)})
        drive = result.data['drives'][0]
        self.assertEqual(drive['percent'], 75.6)
        self.assertEqual(drive['fullness'], 'filling')

    def test_a_tape_in_a_drive_is_described_as_it_is_in_a_slot(self):
        """One rule for both, or the same tape is 'filling' on one card and
        'normal' on the one beside it."""
        from apps.libraries.services.core.units import FULL_AT
        from apps.libraries.services.tapes.models import TapeInfo

        capacity = 480
        used = int(capacity * FULL_AT / 100) + 1
        drive = self._activity({11: self._stats(
            written=used * 1024 * 1024, written_media=used * 1024 * 1024,
            capacity=capacity * 1024 * 1024)}).data['drives'][0]
        tile = TapeInfo(barcode='E01001L8', used_mb=used, capacity_mb=capacity)
        self.assertEqual(drive['fullness'], tile.fullness)
        self.assertEqual(drive['fullness'], 'full')

    def test_a_drive_with_no_tape_has_no_bar(self):
        result = self._activity({11: self._stats(barcode=None, loaded=False)})
        self.assertIsNone(result.data['drives'][0]['percent'])

    def test_an_empty_drive_still_gets_a_row(self):
        result = self._activity({11: self._stats(barcode=None, loaded=False)})
        drive = result.data['drives'][0]
        self.assertEqual(drive['state'], 'empty')
        self.assertEqual(drive['label'], 'empty')
        self.assertEqual(result.data['loaded'], 0)

    def test_a_daemon_that_does_not_answer_is_not_a_failure(self):
        """A stopped drive answers nothing; the other drives still report."""
        result = self._activity({12: self._stats()})
        self.assertTrue(result.success)
        self.assertIsNone(result.data['drives'][0]['stats'])
        self.assertEqual(result.data['drives'][0]['state'], 'silent')
        self.assertEqual(result.data['loaded'], 1)

    def test_settle_zero_never_waits(self):
        """What the web page asks for: the next poll brings the comparison."""
        with mock.patch.object(self.module.time, 'sleep') as slept, \
                mock.patch.object(self.module.vtlcmd, 'stats',
                                  return_value=self._stats()):
            self.module.DriveService(self.config).activity(10, settle=0)
        slept.assert_not_called()

    def test_an_unknown_library_reports_no_drives(self):
        """It follows list(): a library with nothing in device.conf is an
        empty answer, not an error, and the page paints nothing."""
        result = self._activity({}, library_id=99)
        self.assertTrue(result.success)
        self.assertEqual(result.data['drives'], [])
        self.assertEqual(result.data['total'], 0)


class HumanSizeTests(TestCase):
    """One wording for a size, shared by the page and the command line."""

    def test_it_reads_the_way_a_person_says_it(self):
        from apps.libraries.services.core.units import human_size
        self.assertEqual(human_size(0), '0 B')
        self.assertEqual(human_size(None), '0 B')
        self.assertEqual(human_size(512), '512 B')
        self.assertEqual(human_size(2048), '2.0 KB')
        self.assertEqual(human_size(41943040), '40.0 MB')
        self.assertEqual(human_size(314572800), '300.0 MB')
        self.assertEqual(human_size(5 * 1024 ** 4), '5.0 TB')


class SharedSampleTests(TestCase):
    """The last reading of each drive, shared between processes.

    Keeping it in a module dictionary looked enough and was not: the console
    runs three gunicorn workers, so the first polls of a page landed on
    workers that had never seen the drive and called a drive that was plainly
    writing "holding". `mhvtl status activity` is a fourth process again.
    """

    def setUp(self):
        from apps.libraries.services.core import samples
        self.samples = samples
        self.state = Path(tempfile.mkdtemp())
        override = self.settings(MHVTL_GUI_STATE_DIR=str(self.state))
        override.enable()
        self.addCleanup(override.disable)
        samples.forget()

    def test_a_reading_is_there_for_the_next_process(self):
        self.samples.remember({11: {'written': 1024, 'loaded': True}})
        self.assertEqual(self.samples.recent(11)['written'], 1024)

    def test_it_is_written_where_the_setting_says(self):
        self.samples.remember({11: {'written': 1}})
        self.assertEqual(self.samples.path().parent, self.state)
        self.assertTrue((self.state / 'drive-samples.json').exists())

    def test_an_old_reading_is_not_offered(self):
        self.samples.remember({11: {'written': 1024}},
                              now=time.time() - self.samples.LIFE - 1)
        self.assertIsNone(self.samples.recent(11))

    def test_other_drives_are_kept(self):
        self.samples.remember({11: {'written': 1}})
        self.samples.remember({12: {'written': 2}})
        self.assertIsNotNone(self.samples.recent(11))
        self.assertIsNotNone(self.samples.recent(12))

    def test_a_corrupt_file_costs_one_reading_and_nothing_else(self):
        self.samples.path().write_text('{half a fi')
        self.assertIsNone(self.samples.recent(11))
        self.samples.remember({11: {'written': 1}})
        self.assertIsNotNone(self.samples.recent(11))

    def test_a_directory_it_cannot_write_to_is_not_an_error(self):
        """A page must not break because a directory is read-only."""
        with mock.patch.object(self.samples.tempfile, 'NamedTemporaryFile',
                               side_effect=PermissionError('read-only')):
            self.samples.remember({11: {'written': 1}})
        self.assertIsNone(self.samples.recent(11))

    def test_two_people_watching_do_not_confuse_each_other(self):
        """The bug this exists to prevent: the second request arriving a
        moment after the first found a reading identical to its own and
        called a drive that was plainly writing "holding" - so what a drive
        was doing depended on how many people had the page open."""
        from apps.libraries.services.drives import service as drive_service
        from apps.libraries.services.operations.vtlcmd import TapeStats

        written = iter([100, 200, 201, 202, 300])       # MB, climbing
        listed = success_result('1 drive', {'drives': [
            {'drive_id': 51, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_50'}]}, 'op')

        def ask():
            with mock.patch.object(drive_service.DriveService, 'list',
                                   return_value=listed), \
                 mock.patch.object(
                     drive_service.vtlcmd, 'stats',
                     return_value=TapeStats(barcode='K50001L8', loaded=True,
                                            written=next(written) * 1024 * 1024)):
                return drive_service.DriveService(self.state).activity(
                    50, settle=0).data['drives'][0]['state']

        ask()                                    # the first viewer, no history
        # three requests in quick succession, as two open pages produce
        self.assertEqual([ask(), ask(), ask()], ['writing'] * 3)

    def test_two_readings_a_moment_apart_keep_the_last_answer(self):
        """MHVTL updates its counters as buffers are flushed, so two reads a
        fraction of a second apart return the same number from a drive
        writing at full speed. Calling that "holding" made what a drive was
        doing depend on how many people had the page open."""
        from apps.libraries.services.drives import service as drive_service
        from apps.libraries.services.operations.vtlcmd import TapeStats

        listed = success_result('1 drive', {'drives': [
            {'drive_id': 51, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_50'}]}, 'op')

        def ask(written_mb):
            with mock.patch.object(drive_service.DriveService, 'list',
                                   return_value=listed), \
                 mock.patch.object(
                     drive_service.vtlcmd, 'stats',
                     return_value=TapeStats(barcode='K50001L8', loaded=True,
                                            written=written_mb * 1024 * 1024)):
                return drive_service.DriveService(self.state).activity(
                    50, settle=0).data['drives'][0]['state']

        ask(100)
        # far enough apart to tell: it is writing
        self.samples.remember({51: {'written': 100 * 1024 * 1024,
                                    'loaded': True, 'barcode': 'K50001L8',
                                    'state': 'writing'}},
                              now=time.time() - self.samples.MIN_COMPARE - 1,
                              min_age=0)
        self.assertEqual(ask(200), 'writing')

        # a second viewer, a moment later, reading the very same number
        self.assertEqual(ask(200), 'writing',
                         'it did not stop writing in half a second')

    def test_a_drive_that_really_stopped_is_reported_as_stopped(self):
        """Keeping the last answer must not mean keeping it for ever."""
        from apps.libraries.services.drives import service as drive_service
        from apps.libraries.services.operations.vtlcmd import TapeStats

        listed = success_result('1 drive', {'drives': [
            {'drive_id': 51, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_50'}]}, 'op')

        self.samples.remember({51: {'written': 200 * 1024 * 1024,
                                    'loaded': True, 'barcode': 'K50001L8',
                                    'state': 'writing'}},
                              now=time.time() - self.samples.MIN_COMPARE - 1,
                              min_age=0)
        with mock.patch.object(drive_service.DriveService, 'list',
                               return_value=listed), \
             mock.patch.object(
                 drive_service.vtlcmd, 'stats',
                 return_value=TapeStats(barcode='K50001L8', loaded=True,
                                        written=200 * 1024 * 1024)):
            state = drive_service.DriveService(self.state).activity(
                50, settle=0).data['drives'][0]['state']
        self.assertEqual(state, 'holding')

    def test_two_services_see_each_other(self):
        """What the bug was: two callers, one drive, one answer."""
        from apps.libraries.services.drives import service as drive_service
        from apps.libraries.services.operations.vtlcmd import TapeStats

        readings = [TapeStats(barcode='K50001L8', loaded=True, written=28405760),
                    TapeStats(barcode='K50001L8', loaded=True, written=314572800)]
        listed = success_result('1 drive', {'drives': [
            {'drive_id': 51, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_50'}]}, 'op')

        def ask(reading):
            with mock.patch.object(drive_service.DriveService, 'list',
                                   return_value=listed), \
                 mock.patch.object(drive_service.vtlcmd, 'stats',
                                   return_value=reading):
                return drive_service.DriveService(self.state).activity(50, settle=0)

        first = ask(readings[0])                  # "worker one"
        self.assertEqual(first.data['drives'][0]['state'], 'holding')
        second = ask(readings[1])                 # "worker two", fresh memory
        self.assertEqual(second.data['drives'][0]['state'], 'writing')

    def test_the_file_may_be_written_by_the_console_and_by_root(self):
        """`sudo mhvtl` is root and the console is mhvtl-gui, and they share
        this file: root writing first once left one the console could not
        read, so neither could compare two readings."""
        import stat
        self.samples.remember({11: {'written': 1}})
        mode = stat.S_IMODE(self.samples.path().stat().st_mode)
        self.assertTrue(mode & stat.S_IWGRP, oct(mode))
        self.assertTrue(mode & stat.S_IRGRP, oct(mode))
