"""Creating and deleting a library: what gets written, and what gets undone.

Step 8 of the service-layer refactor. Every test writes to a scratch directory
and never to /etc/mhvtl - the reason is in DelegationTests, where a service that
defaulted to the live configuration created library 40 on the running host.

systemd and media deletion are faked. Starting a real vtllibrary@ unit needs the
kernel module and root; deleting real media is the operation that lost 72 tapes
once already, and it does not belong in a test run.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from .base import TestCase

from apps.libraries.services.config import library_contents as contents_format
from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.libraries import lifecycle, spec

FIXTURES = Path(__file__).parent / 'fixtures'

SPEC = {'library_id': 40, 'profile': 'IBM', 'num_drives': 2, 'media_count': 5}


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


class TargetAllocationTests(TestCase):
    def test_takes_the_next_target_above_the_highest_in_use(self):
        text = ('Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00\n'
                'Drive: 11 CHANNEL: 00 TARGET: 01 LUN: 00\n'
                'Drive: 12 CHANNEL: 00 TARGET: 02 LUN: 00\n')
        self.assertEqual(lifecycle.allocate_targets(text, 2), (3, [4, 5]))

    def test_an_empty_configuration_starts_at_zero(self):
        self.assertEqual(lifecycle.allocate_targets('', 1), (0, [1]))

    def test_a_gap_is_left_alone(self):
        """Filling it would split one library's drives across the gap."""
        text = ('Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00\n'
                'Library: 20 CHANNEL: 00 TARGET: 09 LUN: 00\n')
        library_target, drives = lifecycle.allocate_targets(text, 3)
        self.assertEqual(library_target, 10)
        self.assertEqual(drives, [11, 12, 13])


class SpecDefaultTests(TestCase):
    def test_fills_in_the_profile_identity(self):
        filled = spec.apply_defaults({'library_id': 40, 'profile': 'IBM'})
        self.assertTrue(filled['vendor'])
        self.assertTrue(filled['product'])
        self.assertTrue(filled['drive_product'])
        self.assertTrue(filled['media_suffix'])
        self.assertEqual(filled['channel'], 0)

    def test_an_explicit_value_is_never_overwritten(self):
        filled = spec.apply_defaults({'library_id': 40, 'profile': 'IBM',
                                      'num_drives': 7, 'media_count': 12})
        self.assertEqual(filled['num_drives'], 7)
        self.assertEqual(filled['media_count'], 12)

    def test_the_input_is_not_modified(self):
        original = {'library_id': 40, 'profile': 'IBM'}
        spec.apply_defaults(original)
        self.assertEqual(original, {'library_id': 40, 'profile': 'IBM'})

    def test_the_barcode_prefix_carries_the_library_id(self):
        filled = spec.apply_defaults({'library_id': 40, 'profile': 'IBM'})
        self.assertIn('40', filled['barcode_prefix'])

    def test_an_unknown_profile_is_refused_with_the_valid_ones(self):
        with self.assertRaises(spec.UnknownProfile) as refusal:
            spec.apply_defaults({'library_id': 40, 'profile': 'acme'})
        self.assertIn('IBM', str(refusal.exception))

    def test_a_missing_profile_is_refused(self):
        with self.assertRaises(spec.UnknownProfile):
            spec.apply_defaults({'library_id': 40})

    def test_the_profile_key_is_read_under_any_of_its_names(self):
        for key in ('profile', 'vendor_profile', 'vendor_key'):
            with self.subTest(key=key):
                self.assertEqual(
                    spec.apply_defaults({'library_id': 40, key: 'IBM'})['profile'],
                    'IBM')

    def test_an_id_is_allocated_only_when_asked_for(self):
        filled = spec.apply_defaults({'profile': 'IBM'}, next_id=lambda: 60)
        self.assertEqual(filled['library_id'], 60)
        with self.assertRaises(spec.UnknownProfile):
            spec.apply_defaults({'profile': 'IBM'})

    def test_a_serial_longer_than_the_scsi_field_is_cut(self):
        filled = spec.apply_defaults({'library_id': 40, 'profile': 'IBM',
                                      'serial': 'A' * 40})
        self.assertEqual(len(filled['serial']), spec.MAX_SERIAL_LENGTH)

    def test_drive_ids_follow_the_library_id(self):
        self.assertEqual(spec.drive_ids(40, 3), [41, 42, 43])


class ContentsRenderingTests(TestCase):
    def test_slots_are_numbered_from_one_with_no_gaps(self):
        """MHVTL stops reading at the first missing slot number."""
        text = contents_format.render_new(40, 2, barcode_prefix='M40',
                                          media_suffix='L8', media_count=3,
                                          empty_slots=2)
        numbers = [slot.number for slot in contents_format.parse(text).slots]
        self.assertEqual(numbers, [1, 2, 3, 4, 5])

    def test_barcodes_are_prefix_number_suffix(self):
        text = contents_format.render_new(40, 1, barcode_prefix='M40',
                                          media_suffix='L8', media_count=2)
        self.assertEqual(contents_format.parse(text).barcodes,
                         ['M40001L8', 'M40002L8'])

    def test_what_is_written_can_be_read_back(self):
        text = contents_format.render_new(40, 3, media_count=4, empty_slots=1)
        parsed = contents_format.parse(text)
        self.assertEqual(parsed.version, 2)
        self.assertEqual(parsed.drive_count, 3)
        self.assertEqual(len(parsed.occupied), 4)
        self.assertEqual(len(parsed.slots), 5)

    def test_the_barcode_legend_is_kept(self):
        """Every other library_contents on the system carries it, and an
        operator editing the file by hand reads the suffix table from it."""
        self.assertIn('Trailing "TA"', contents_format.render_new(40, 1))


class MixedSpecTests(TestCase):
    """One canonical form for a library that holds more than one kind.

    `--drive MODEL:COUNT` repeated, and a preset's [[name.drive]] array, are
    the same list by the time they reach here. apply_defaults is the only
    thing that builds drive_slots and media_runs from it, which is why the
    writers below have one path each rather than a uniform case and a mixed
    one free to disagree.
    """

    MIXED = {'library_id': 40, 'profile': 'IBM', 'library_model': '03584L32',
             'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                       {'model': 'ULT3580-TD6', 'count': 2}],
             'media': [{'density': 'LTO8', 'count': 3},
                       {'density': 'LTO6', 'count': 2}]}

    def test_one_slot_per_drive_in_the_order_asked_for(self):
        filled = spec.apply_defaults(self.MIXED)
        self.assertEqual([slot['product'] for slot in filled['drive_slots']],
                         ['ULT3580-TD8', 'ULT3580-TD8',
                          'ULT3580-TD6', 'ULT3580-TD6'])

    def test_the_list_decides_how_many_drives_there_are(self):
        """Not the other way round: num_drives is derived, so nothing can ask
        for four drives and list two."""
        filled = spec.apply_defaults(self.MIXED)
        self.assertEqual(filled['num_drives'], 4)
        self.assertEqual(filled['media_count'], 5)

    def test_the_nominal_density_is_the_first_one_a_drive_writes(self):
        """The rule `mhvtl tape media` reports for a library that exists, so a
        mixed library is described the same way before and after creation."""
        filled = spec.apply_defaults(self.MIXED)
        self.assertEqual(filled['media_type'], 'LTO8')
        self.assertEqual(filled['media_suffix'], 'L8')

    def test_a_uniform_specification_still_fills_its_slots(self):
        """The same list, one model repeated: no second path through here."""
        filled = spec.apply_defaults({'library_id': 40, 'profile': 'IBM',
                                      'num_drives': 3})
        self.assertEqual(len(filled['drive_slots']), 3)
        self.assertEqual({slot['product'] for slot in filled['drive_slots']},
                         {filled['drive_product']})
        self.assertEqual([run['count'] for run in filled['media_runs']],
                         [filled['media_count']])

    def test_the_counts_asked_for_are_read_from_whichever_shape_arrived(self):
        """What validation asks before it checks a layout limit. A preset
        holding a list has no num_drives at all, and reading the key alone
        said four - the default - for a library of twelve."""
        self.assertEqual(spec.asked_drive_count(self.MIXED, 4), 4)
        self.assertEqual(spec.asked_media_count(self.MIXED, 50), 5)
        self.assertEqual(spec.asked_drive_models(self.MIXED),
                         ['ULT3580-TD8', 'ULT3580-TD6'])
        self.assertEqual(spec.asked_densities(self.MIXED), ['LTO8', 'LTO6'])

    def test_a_filled_specification_answers_the_same(self):
        filled = spec.apply_defaults(self.MIXED)
        self.assertEqual(spec.asked_drive_count(filled, 4), 4)
        self.assertEqual(spec.asked_media_count(filled, 50), 5)
        self.assertEqual(spec.asked_drive_models(filled),
                         ['ULT3580-TD8', 'ULT3580-TD6'])
        self.assertEqual(spec.asked_densities(filled), ['LTO8', 'LTO6'])


class RunSyntaxTests(TestCase):
    """``MODEL:COUNT``, the one syntax both front ends ask in.

    It was mhvtl_cli's while the command line was the only caller. The setup
    form's rows send the same thing back to the server when a row changes,
    and the web cannot import mhvtl_cli - so a second encoding of one syntax
    was the alternative, free to disagree with the first.
    """

    def test_a_name_and_a_count(self):
        self.assertEqual(spec.parse_runs(['ULT3580-TD8:2'], 'model'),
                         [{'model': 'ULT3580-TD8', 'count': 2}])

    def test_no_count_means_one(self):
        self.assertEqual(spec.parse_runs(['ULT3580-TD8', 'ULT3580-TD6'],
                                         'model'),
                         [{'model': 'ULT3580-TD8', 'count': 1},
                          {'model': 'ULT3580-TD6', 'count': 1}])

    def test_the_order_given_is_the_order_kept(self):
        """Slot order, and therefore SCSI target order."""
        self.assertEqual([run['density'] for run in
                          spec.parse_runs(['LTO6:1', 'LTO8:2'], 'density')],
                         ['LTO6', 'LTO8'])

    def test_what_it_refuses(self):
        for bad, why in ((':2', 'names nothing'),
                         ('ULT3580-TD8:x', 'not a whole number'),
                         ('ULT3580-TD8:0', 'creates nothing'),
                         ('ULT3580-TD8:-1', 'creates nothing')):
            with self.subTest(given=bad):
                with self.assertRaises(ValueError) as refused:
                    spec.parse_runs([bad], 'model')
                self.assertIn(why, str(refused.exception))

    def test_what_it_parses_is_what_apply_defaults_takes(self):
        """The round trip that matters: the syntax reaches the slots."""
        filled = spec.apply_defaults({
            'library_id': 40, 'profile': 'IBM', 'library_model': '03584L32',
            'drive': spec.parse_runs(['ULT3580-TD8:2', 'ULT3580-TD6:1'],
                                     'model'),
            'media': spec.parse_runs(['LTO8:3'], 'density')})
        self.assertEqual([slot['product'] for slot in filled['drive_slots']],
                         ['ULT3580-TD8', 'ULT3580-TD8', 'ULT3580-TD6'])
        self.assertEqual(filled['media_count'], 3)


class MixedRenderingTests(TestCase):
    """What the two files look like for a mixed library."""

    def _filled(self):
        return spec.apply_defaults(MixedSpecTests.MIXED)

    def test_device_conf_writes_each_slot_its_own_model(self):
        """It took the model from outside the loop until 4 October 2026, so a
        mixed library was written as four of whatever the first slot had."""
        from apps.libraries.services.config import device_conf

        text = device_conf.render_library_and_drives(
            '', self._filled() | {'target': 0}, [1, 2, 3, 4])
        models = [line.split(':', 1)[1].strip() for line in text.splitlines()
                  if line.startswith(' Product identification:')]
        self.assertEqual(models, ['03584L32', 'ULT3580-TD8', 'ULT3580-TD8',
                                  'ULT3580-TD6', 'ULT3580-TD6'])

    def test_each_drive_slot_reads_back_with_the_model_asked_for(self):
        """Through the parser, because the slot number is how MHVTL and the
        database tie a drive to its position - and position is what a backup
        application addresses a drive by."""
        from apps.libraries.services.config import device_conf

        text = device_conf.render_library_and_drives(
            '', self._filled() | {'target': 0}, [1, 2, 3, 4])
        parsed = device_conf.parse(text)
        self.assertEqual([(drive['slot'], drive['product'])
                          for drive in parsed.drives.values()],
                         [(1, 'ULT3580-TD8'), (2, 'ULT3580-TD8'),
                          (3, 'ULT3580-TD6'), (4, 'ULT3580-TD6')])

    def test_the_barcodes_change_suffix_without_restarting_the_numbers(self):
        """MHVTL reads the density out of the suffix and nothing else, and two
        cartridges with one barcode is a library that cannot be inventoried."""
        filled = self._filled()
        text = contents_format.render_new(
            40, filled['num_drives'], barcode_prefix=filled['barcode_prefix'],
            media_count=filled['media_count'], empty_slots=2,
            media_runs=filled['media_runs'])
        self.assertEqual(contents_format.parse(text).barcodes,
                         ['I40001L8', 'I40002L8', 'I40003L8',
                          'I40004L6', 'I40005L6'])

    def test_the_empty_slots_follow_the_last_run(self):
        filled = self._filled()
        text = contents_format.render_new(
            40, filled['num_drives'], barcode_prefix=filled['barcode_prefix'],
            media_count=filled['media_count'], empty_slots=2,
            media_runs=filled['media_runs'])
        parsed = contents_format.parse(text)
        self.assertEqual([slot.number for slot in parsed.slots],
                         [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(len(parsed.occupied), 5)


def as_live(test, directory):
    """Make a scratch directory look like the live one.

    lifecycle and orphans only start or stop daemons for the configuration
    directory the daemons actually read, so a test that checks the units are
    controlled has to say that this is that directory.

    BOTH names, because there are two: config_dir() is where this process reads
    and writes, and daemon_config_dir() is where the daemons read. They are the
    same unless something says otherwise - the CLI does, when --config-dir
    points at a copy - and daemons_are_ours() compares against the second. A
    helper that pinned only the first left daemons_are_ours() asking the real
    setting, so the units went uncontrolled and five tests failed on the
    assertion rather than on the behaviour.
    """
    for name in ('config_dir', 'daemon_config_dir'):
        patch = mock.patch.object(lifecycle, name, return_value=directory)
        patch.start()
        test.addCleanup(patch.stop)


class CreateTests(TestCase):
    """The write path, against a scratch /etc/mhvtl."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)
        as_live(self, self.config)
        patch = mock.patch.object(lifecycle.units, 'start_library',
                                  return_value={})
        self.started = patch.start()
        self.addCleanup(patch.stop)

    def _create(self, **overrides):
        return lifecycle.create({**SPEC, **overrides}, self.config)

    def test_writes_both_files(self):
        result = self._create()
        self.assertTrue(result.success, result.errors)
        self.assertTrue((self.config / 'library_contents.40').exists())
        self.assertIn('Library: 40', (self.config / 'device.conf').read_text())

    def test_the_library_and_its_drives_are_readable_afterwards(self):
        self._create()
        conf = lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text())
        self.assertIn(40, conf.libraries)
        self.assertEqual(sorted(conf.drives_of(40)), [41, 42])

    def test_the_existing_libraries_survive(self):
        """Appending, not rewriting: the fixture has three libraries."""
        before = lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text()).libraries
        self._create()
        after = lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text()).libraries
        self.assertTrue(set(before) < set(after))

    def test_targets_do_not_collide_with_what_is_there(self):
        before = lifecycle.device_conf_format.parse(
            (self.config / "device.conf").read_text()).used_targets()
        result = self._create()
        self.assertNotIn(result.data['target'], before)
        for target in result.data['drive_targets']:
            self.assertNotIn(target, before)

    def test_a_backup_is_taken_before_anything_is_written(self):
        result = self._create()
        self.assertTrue(Path(result.data['backup_path'], 'device.conf').exists())

    def test_a_mixed_library_is_created_as_asked_in_both_files(self):
        """The whole of mixed drives and media, through the operation: two
        LTO-8 drives, two LTO-6 drives, and cartridges for both."""
        result = lifecycle.create(MixedSpecTests.MIXED, self.config)
        self.assertTrue(result.success, result.errors)

        conf = lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text())
        drives = [conf.drives[did] for did in sorted(conf.drives_of(40))]
        self.assertEqual([drive['product'] for drive in drives],
                         ['ULT3580-TD8', 'ULT3580-TD8',
                          'ULT3580-TD6', 'ULT3580-TD6'])

        contents = contents_format.parse(
            (self.config / 'library_contents.40').read_text())
        self.assertEqual(contents.drive_count, 4)
        self.assertEqual(contents.barcodes,
                         ['I40001L8', 'I40002L8', 'I40003L8',
                          'I40004L6', 'I40005L6'])

    def test_an_id_already_in_use_is_refused(self):
        result = self._create(library_id=10)
        self.assertFalse(result.success)
        self.assertIn('already exists', ' '.join(result.errors))

    def test_an_unknown_profile_is_refused_before_any_write(self):
        before = (self.config / 'device.conf').read_text()
        result = lifecycle.create({'library_id': 40, 'profile': 'acme'}, self.config)
        self.assertFalse(result.success)
        self.assertEqual((self.config / 'device.conf').read_text(), before)

    def test_media_the_drive_cannot_read_is_refused(self):
        result = self._create(media_type='LTO4')
        self.assertFalse(result.success)
        self.assertIn('not supported', ' '.join(result.errors))
        self.assertNotIn('Library: 40', (self.config / 'device.conf').read_text())

    def test_the_units_are_started_for_the_drives_that_were_created(self):
        self._create(num_drives=3)
        self.started.assert_called_once_with(40, [41, 42, 43])

    def test_starting_can_be_skipped(self):
        lifecycle.create({**SPEC}, self.config, start_services=False)
        self.started.assert_not_called()

    def test_a_unit_that_will_not_start_is_reported_not_fatal(self):
        """The configuration is written and correct by then."""
        self.started.return_value = {'vtltape@41.service': False}
        result = self._create()
        self.assertTrue(result.success)
        self.assertEqual(result.data['units_failed'], ['vtltape@41.service'])
        self.assertIn('did not start', result.message)


class CreateRollbackTests(TestCase):
    """A create that fails part way through must leave nothing behind."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.before = (self.config / 'device.conf').read_text()
        as_live(self, self.config)
        patch = mock.patch.object(lifecycle.units, 'start_library', return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_failed_contents_write_restores_device_conf(self):
        from apps.libraries.services.config.service import ConfigService

        def write_contents(self, library_id, text, backup=True):
            from apps.libraries.services.core import failure_result
            return failure_result('disk full', ['ENOSPC'], 'test')

        with mock.patch.object(ConfigService, 'write_library_contents',
                               write_contents):
            result = lifecycle.create(dict(SPEC), self.config)

        self.assertFalse(result.success)
        self.assertEqual((self.config / 'device.conf').read_text(), self.before)
        self.assertFalse((self.config / 'library_contents.40').exists())

    def test_the_rollback_is_reported_not_silent(self):
        from apps.libraries.services.config.service import ConfigService

        with mock.patch.object(ConfigService, 'write_library_contents',
                               side_effect=OSError('read-only file system')):
            result = lifecycle.create(dict(SPEC), self.config)

        self.assertFalse(result.success)
        self.assertIn('restored', ' '.join(result.errors))


class DeleteTests(TestCase):
    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10', 'library_contents.20',
                     'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)

        as_live(self, self.config)
        stop = mock.patch.object(lifecycle.units, 'stop_library', return_value={})
        self.stopped = stop.start()
        self.addCleanup(stop.stop)
        forget = mock.patch.object(lifecycle.units, 'forget_library')
        self.forgotten = forget.start()
        self.addCleanup(forget.stop)

        safe = mock.patch.object(lifecycle, 'check_safe_to_delete',
                                 return_value=(True, 'offline', []))
        self.safety = safe.start()
        self.addCleanup(safe.stop)

    def _conf(self):
        return lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text())

    def test_removes_the_library_its_drives_and_its_contents_file(self):
        drives = sorted(self._conf().drives_of(10))
        result = lifecycle.delete(10, config_directory=self.config)

        self.assertTrue(result.success, result.errors)
        conf = self._conf()
        self.assertNotIn(10, conf.libraries)
        for drive_id in drives:
            self.assertNotIn(drive_id, conf.drives)
        self.assertFalse((self.config / 'library_contents.10').exists())

    def test_systemd_is_reloaded_after_the_library_leaves_device_conf(self):
        """MHVTL's generator links a unit for every device.conf entry at each
        daemon-reload; reloading only before the entry was gone left the
        removed library's units enabled until reboot."""
        drives = sorted(self._conf().drives_of(10))
        seen = {}

        def forget(library_id, drive_ids):
            seen['library'] = library_id
            seen['drives'] = sorted(drive_ids)
            seen['still_there'] = 10 in self._conf().libraries

        self.forgotten.side_effect = forget
        self.assertTrue(lifecycle.delete(10, config_directory=self.config).success)
        self.assertEqual(seen, {'library': 10, 'drives': drives, 'still_there': False})

    def test_the_other_libraries_are_untouched(self):
        lifecycle.delete(10, config_directory=self.config)
        conf = self._conf()
        self.assertIn(20, conf.libraries)
        self.assertIn(30, conf.libraries)
        self.assertTrue((self.config / 'library_contents.20').exists())

    def test_the_daemons_are_stopped_before_the_configuration_goes(self):
        """A daemon whose device.conf entry has vanished logs until it is killed."""
        lifecycle.delete(10, config_directory=self.config)
        self.stopped.assert_called_once()
        self.assertEqual(self.stopped.call_args[0][0], 10)

    def test_an_unknown_library_is_refused(self):
        result = lifecycle.delete(99, config_directory=self.config)
        self.assertFalse(result.success)
        self.assertIn('does not exist', result.message)

    def test_media_is_kept_unless_asked_for(self):
        with mock.patch.object(lifecycle, '_remove_media') as remove:
            lifecycle.delete(10, config_directory=self.config)
        remove.assert_not_called()

    def test_media_is_removed_when_asked_for(self):
        with mock.patch.object(lifecycle, '_remove_media',
                               return_value=(3, [])) as remove:
            result = lifecycle.delete(10, remove_media=True,
                                      config_directory=self.config)
        self.assertTrue(remove.call_args[0][0],
                        'barcodes must be read before the contents file is removed')
        self.assertEqual(result.data['media_removed'], 3)

    def test_a_loaded_drive_stops_the_delete(self):
        self.safety.return_value = (False, '1 drive(s) still have tapes loaded',
                                    [{'number': 1, 'barcode': 'E01001L8'}])
        result = lifecycle.delete(10, config_directory=self.config)

        self.assertFalse(result.success)
        self.assertTrue(result.data['requires_force'])
        self.assertIn(10, self._conf().libraries)

    def test_force_goes_ahead_without_checking(self):
        result = lifecycle.delete(10, force=True, config_directory=self.config)
        self.assertTrue(result.success)
        self.safety.assert_not_called()

    def test_a_backup_is_taken_first(self):
        result = lifecycle.delete(10, config_directory=self.config)
        self.assertTrue(Path(result.data['backup_path'], 'device.conf').exists())


class DeleteRollbackTests(TestCase):
    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.before = (self.config / 'device.conf').read_text()
        as_live(self, self.config)

        for name in ('stop_library', 'start_library', 'forget_library'):
            patch = mock.patch.object(lifecycle.units, name, return_value={})
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

        safe = mock.patch.object(lifecycle, 'check_safe_to_delete',
                                 return_value=(True, 'offline', []))
        safe.start()
        self.addCleanup(safe.stop)

    def test_a_failed_write_restores_the_configuration_and_the_daemons(self):
        from apps.libraries.services.config.service import ConfigService

        with mock.patch.object(ConfigService, 'write_device_conf',
                               side_effect=OSError('read-only file system')):
            result = lifecycle.delete(10, config_directory=self.config)

        self.assertFalse(result.success)
        self.assertEqual((self.config / 'device.conf').read_text(), self.before)
        self.assertTrue((self.config / 'library_contents.10').exists(),
                        'the contents file must not be removed after a failure')
        self.start_library.assert_called_once()


class SafetyCheckTests(TestCase):
    """An offline library is safe; a loaded drive is not."""

    def _status(self, result):
        from apps.libraries.services.operations.service import OperationsService
        return mock.patch.object(OperationsService, 'status', return_value=result)

    def test_a_loaded_drive_is_refused_and_named(self):
        from apps.libraries.services.core import success_result
        status = success_result('ok', {'drives': [
            {'number': 1, 'full': True, 'barcode': 'E01001L8'},
            {'number': 2, 'full': False, 'barcode': None}]})

        with self._status(status):
            safe, reason, loaded = lifecycle.check_safe_to_delete(10)

        self.assertFalse(safe)
        self.assertIn('E01001L8', reason)
        self.assertEqual(len(loaded), 1)

    def test_empty_drives_are_safe(self):
        from apps.libraries.services.core import success_result
        with self._status(success_result('ok', {'drives': [
                {'number': 1, 'full': False}]})):
            self.assertTrue(lifecycle.check_safe_to_delete(10)[0])

    def test_an_offline_library_is_safe(self):
        """Its daemons are already down; that is where a delete is heading."""
        from apps.libraries.services.core import failure_result
        with self._status(failure_result('no device', ['offline'])):
            safe, reason, loaded = lifecycle.check_safe_to_delete(10)
        self.assertTrue(safe)
        self.assertEqual(loaded, [])

    def test_a_raising_status_call_does_not_block_a_delete(self):
        from apps.libraries.services.operations.service import OperationsService
        with mock.patch.object(OperationsService, 'status',
                               side_effect=OSError('mtx missing')):
            safe, reason, _ = lifecycle.check_safe_to_delete(10)
        self.assertTrue(safe)
        self.assertIn('mtx missing', reason)


class UpdateTests(TestCase):
    """An update is a delete and a create, under one lock and one backup."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)
        shutil.copy(FIXTURES / 'library_contents.10', self.config)

        for name in ('start_library', 'stop_library'):
            patch = mock.patch.object(lifecycle.units, name, return_value={})
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

        safe = mock.patch.object(lifecycle, 'check_safe_to_delete',
                                 return_value=(True, 'offline', []))
        safe.start()
        self.addCleanup(safe.stop)

    def _conf(self):
        return lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text())

    def test_the_library_keeps_its_id_and_takes_the_new_settings(self):
        result = lifecycle.update(10, {'profile': 'IBM', 'num_drives': 2,
                                       'media_count': 3}, self.config)
        self.assertTrue(result.success, result.errors)

        conf = self._conf()
        self.assertIn(10, conf.libraries)
        self.assertEqual(len(conf.drives_of(10)), 2)

    def test_the_id_in_the_spec_cannot_move_the_library(self):
        """Updating library 10 must not create library 50."""
        lifecycle.update(10, {'profile': 'IBM', 'library_id': 50}, self.config)
        conf = self._conf()
        self.assertIn(10, conf.libraries)
        self.assertNotIn(50, conf.libraries)

    def test_the_other_libraries_are_untouched(self):
        lifecycle.update(10, {'profile': 'IBM', 'num_drives': 1}, self.config)
        conf = self._conf()
        self.assertIn(20, conf.libraries)
        self.assertIn(30, conf.libraries)

    def test_the_contents_file_is_regenerated_and_says_so(self):
        result = lifecycle.update(10, {'profile': 'IBM', 'media_count': 3},
                                  self.config)
        self.assertTrue(result.data['library_contents_regenerated'])
        contents = lifecycle.library_contents_format.parse(
            (self.config / 'library_contents.10').read_text())
        self.assertEqual(len(contents.occupied), 3)

    def test_a_rejected_specification_leaves_the_library_as_it_was(self):
        before = (self.config / 'device.conf').read_text()
        result = lifecycle.update(10, {'profile': 'acme'}, self.config)

        self.assertFalse(result.success)
        self.assertEqual((self.config / 'device.conf').read_text(), before)
        self.assertIn('restored', ' '.join(result.errors))

    def test_an_unknown_library_is_refused_before_the_backup(self):
        before = (self.config / 'device.conf').read_text()
        result = lifecycle.update(99, {'profile': 'IBM'}, self.config)
        self.assertFalse(result.success)
        self.assertEqual((self.config / 'device.conf').read_text(), before)


class RecognitionTests(TestCase):
    """Three things have to agree before a library is really there."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)
        shutil.copy(FIXTURES / 'library_contents.10', self.config)

    def _active(self, value):
        return mock.patch.object(lifecycle.units, 'is_active', return_value=value)

    def test_all_three_present(self):
        with self._active(True):
            self.assertTrue(lifecycle.recognised(10, self.config))

    def test_a_stopped_daemon_is_not_recognised(self):
        with self._active(False):
            self.assertFalse(lifecycle.recognised(10, self.config))

    def test_a_missing_contents_file_is_not_recognised(self):
        """The state after a create that failed at its last step."""
        with self._active(True):
            self.assertFalse(lifecycle.recognised(20, self.config))

    def test_a_library_not_in_device_conf_is_not_recognised(self):
        with self._active(True):
            self.assertFalse(lifecycle.recognised(99, self.config))


class NewHostTests(TestCase):
    """The first library on a host that has no device.conf at all."""

    def setUp(self):
        self.config = self.tmpdir()
        patch = mock.patch.object(lifecycle.units, 'start_library', return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_first_library_is_created(self):
        result = lifecycle.create(dict(SPEC), self.config)
        self.assertTrue(result.success, result.errors)
        self.assertIn(40, lifecycle.device_conf_format.parse(
            (self.config / 'device.conf').read_text()).libraries)

    def test_the_file_gets_mhvtls_own_header(self):
        """Without VERSION: 5 the daemons read a file unlike every other host's."""
        lifecycle.create(dict(SPEC), self.config)
        self.assertTrue((self.config / 'device.conf').read_text()
                        .startswith('VERSION: 5'))

    def test_the_header_is_not_added_twice(self):
        lifecycle.create(dict(SPEC), self.config)
        lifecycle.create({**SPEC, 'library_id': 50}, self.config)
        self.assertEqual((self.config / 'device.conf').read_text()
                         .count('VERSION: 5'), 1)

    def test_targets_start_from_zero(self):
        result = lifecycle.create(dict(SPEC), self.config)
        self.assertEqual(result.data['target'], 0)


class DaemonsBelongToTheLiveDirectoryTests(TestCase):
    """A scratch configuration directory never starts or stops a daemon.

    The daemons read the live /etc/mhvtl. An operation writing anywhere else -
    a test, a scratch copy - that starts or stops units applies a file they do
    not read and, worse, stops libraries that are in use: adding a drive in a
    test stopped a running drive on this host once.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)

    def test_create_does_not_start_units_for_a_scratch_directory(self):
        with mock.patch.object(lifecycle.units, 'start_library') as started:
            result = lifecycle.create({**SPEC, 'library_id': 40}, self.config,
                                      start_services=True)
        self.assertTrue(result.success, result.message)
        started.assert_not_called()

    def test_delete_does_not_stop_units_for_a_scratch_directory(self):
        with mock.patch.object(lifecycle, 'check_safe_to_delete',
                               return_value=(True, 'offline', [])), \
                mock.patch.object(lifecycle.units, 'stop_library') as stopped:
            result = lifecycle.delete(10, config_directory=self.config)
        self.assertTrue(result.success, result.message)
        stopped.assert_not_called()

    def test_the_live_directory_is_recognised(self):
        from apps.libraries.services.core import config_dir
        self.assertTrue(lifecycle.daemons_are_ours(config_dir()))
        self.assertFalse(lifecycle.daemons_are_ours(self.config))


class EmptySlotTests(TestCase):
    """Changing how many empty slots a library has, keeping its tapes.

    Slots live in library_contents, which the daemon reads once at start; this
    writes the file and reports that a restart is needed. Nothing here touches
    device.conf or any media.
    """
    CONF = ('Library: 40 CHANNEL: 00 TARGET: 18 LUN: 00\n'
            ' Vendor identification: IBM\n'
            ' Product identification: 03584L32\n')

    def setUp(self):
        self.config = self.tmpdir()
        self.addCleanup(shutil.rmtree, self.config, True)
        (self.config / 'device.conf').write_text(self.CONF)
        self.write(['Slot 1: E40001L8', 'Slot 2: E40002L8', 'Slot 3:'])

    def write(self, slot_lines):
        (self.config / 'library_contents.40').write_text(
            'VERSION: 2\n\nDrive 1:\n\nPicker 1:\n\nMAP 1:\n\n'
            + '\n'.join(slot_lines) + '\n')

    def contents(self):
        return contents_format.parse(
            (self.config / 'library_contents.40').read_text())

    def test_growing_appends_empty_slots_after_the_last(self):
        result = lifecycle.set_empty_slots(40, 4, self.config)
        self.assertTrue(result.success, result.message)
        slots = self.contents().slots
        self.assertEqual([s.number for s in slots], [1, 2, 3, 4, 5, 6])
        self.assertEqual([s.barcode for s in slots[:2]], ['E40001L8', 'E40002L8'])
        self.assertEqual(result.data['empty_slots'], 4)
        self.assertEqual(result.data['total_slots'], 6)

    def test_the_tapes_keep_their_slot_numbers(self):
        """Renumbering would move a tape the robot already reports at an
        address."""
        lifecycle.set_empty_slots(40, 0, self.config)
        slots = self.contents().slots
        self.assertEqual([(s.number, s.barcode) for s in slots],
                         [(1, 'E40001L8'), (2, 'E40002L8')])

    def test_shrinking_removes_from_the_end(self):
        self.write(['Slot 1: E40001L8', 'Slot 2:', 'Slot 3:', 'Slot 4:'])
        result = lifecycle.set_empty_slots(40, 1, self.config)
        self.assertTrue(result.success, result.message)
        self.assertEqual([s.number for s in self.contents().slots], [1, 2])

    def test_it_will_not_leave_a_gap_in_the_middle(self):
        """MHVTL stops reading at the first missing slot number, so removing an
        empty slot with a tape after it would silently shorten the library."""
        self.write(['Slot 1:', 'Slot 2: E40002L8'])
        result = lifecycle.set_empty_slots(40, 0, self.config)
        self.assertFalse(result.success)
        self.assertIn('gap', ' '.join(result.errors))
        self.assertEqual(len(self.contents().slots), 2)

    def test_it_refuses_more_slots_than_the_model_holds(self):
        result = lifecycle.set_empty_slots(40, 100000, self.config)
        self.assertFalse(result.success)
        self.assertIn('at most', ' '.join(result.errors))

    def test_a_negative_count_is_refused(self):
        self.assertFalse(lifecycle.set_empty_slots(40, -1, self.config).success)

    def test_an_unreadable_library_is_a_failure(self):
        self.assertFalse(lifecycle.set_empty_slots(99, 2, self.config).success)

    def test_it_says_a_restart_is_needed(self):
        result = lifecycle.set_empty_slots(40, 5, self.config)
        self.assertTrue(result.data['restart_required'])
