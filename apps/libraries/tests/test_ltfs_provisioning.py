"""add_ltfs_drive_workflow(): a library whose drives LTFS refuses, fixed in place.

Library 50 is the real case - two LTO-8 drives that LTFS will not open, because
the STK profile labels them vendor `STK` and LTFS has no STK table. Before this,
the only fix was deleting the library, which throws away its cartridges.

The tests run against a copy of the fixture configuration; the steps that would
touch systemd or make media files are patched, because what is under test is the
order of the decisions and which of them refuse.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from .base import TestCase

from apps.libraries.services.libraries import workflow as workflow_module
from apps.libraries.services.libraries import add_ltfs_drive_workflow
from apps.libraries.services.libraries.service import LibraryService
from apps.libraries.services.config import library_contents as contents_format
from apps.libraries.services.config.service import ConfigService
from apps.libraries.services.tapes import TapeService
from apps.libraries.services.core.results import success_result

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
CONFIG_FILES = [p.name for p in FIXTURES.iterdir() if p.is_file()]


class LtfsDriveWorkflowTests(TestCase):

    def setUp(self):
        self.config = self.tmpdir()
        for name in CONFIG_FILES:
            shutil.copy(FIXTURES / name, self.config)

        patches = [
            mock.patch.object(LibraryService, 'restart_services',
                              return_value=success_result('Services restarted')),
            mock.patch.object(workflow_module, '_create_ltfs_media',
                              return_value=('media', True, '2 tape(s) created')),
            mock.patch.object(workflow_module, '_record',
                              return_value=('record', True, 'recorded')),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])

    def run_it(self, library_id=10, **kwargs):
        kwargs.setdefault('config_directory', self.config)
        return add_ltfs_drive_workflow(library_id, **kwargs)

    def steps(self, result):
        return {step['step']: step for step in result.data['steps']}

    def drives(self, library_id=10):
        conf = ConfigService(self.config).device_conf()
        return conf.drives_of(library_id)

    # -- the happy path ----------------------------------------------------

    def test_adds_a_drive_ltfs_can_open(self):
        result = self.run_it()
        self.assertTrue(result.success, result.message)
        self.assertEqual([s['step'] for s in result.data['steps']],
                         ['validate', 'slots', 'drive', 'restart', 'record',
                          'verify'])
        added = self.drives()[result.data['drive_id']]
        # The fixture's library 10 is an STK L700: its profile takes
        # ULT3580-TD8, and the vendor id is what LTFS refuses. So the model
        # stays and the vendor id becomes one LTFS knows.
        self.assertEqual(added['product'], 'ULT3580-TD8')
        self.assertIn(added['vendor'], ('IBM', 'HP', 'HPE', 'QUANTUM'))
        self.assertNotEqual(added['vendor'], 'STK')

    def test_the_verify_step_reads_the_file_back(self):
        """Success is what device.conf says afterwards, not what was asked."""
        result = self.run_it()
        self.assertTrue(self.steps(result)['verify']['ok'])
        self.assertIn('LTFS can open', self.steps(result)['verify']['message'])

    def test_a_revision_is_written_and_verified(self):
        result = self.run_it(vendor='IBM', model='ULT3580-TD8', revision='HB82')
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.drives()[result.data['drive_id']]['revision'],
                         'HB82')
        self.assertIn('rev HB82', self.steps(result)['verify']['message'])

    def test_it_keeps_the_model_and_changes_the_vendor_id(self):
        """Library 50's case, built exactly: STK drives in an STK library.

        The profile takes ULT3580-TD8 because a StorageTek library really does
        hold drives with IBM part numbers. Only the vendor id `STK` is what LTFS
        will not have, and the drive is emulated, so that id is ours to choose.

        The fixture's drives already report IBM, so this rewrites them to STK -
        which is what device.conf says on the real library 50.
        """
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace(
            ' Vendor identification: IBM\n', ' Vendor identification: STK\n'))
        result = self.run_it()
        self.assertTrue(result.success, result.message)
        validate = self.steps(result)['validate']
        self.assertTrue(validate['ok'])
        self.assertIn('rather than STK', validate['message'])

    def test_a_model_no_ltfs_vendor_lists_is_refused_with_the_alternative(self):
        result = self.run_it(model='T10000B')
        self.assertFalse(result.success)
        self.assertIn('T10000B', result.message)
        self.assertIn('this library takes', ' '.join(result.errors))

    # -- the refusals, all before anything is written -----------------------

    def test_a_model_ltfs_refuses_is_refused_first(self):
        before = set(self.drives())
        result = self.run_it(vendor='IBM', model='ULT3580-TD8', revision='D.02')
        self.assertFalse(result.success)
        self.assertIn('HB81', self.steps(result)['validate']['message'])
        self.assertEqual(set(self.drives()), before, 'nothing may be written')

    def test_a_vendor_ltfs_does_not_know_is_refused(self):
        result = self.run_it(vendor='STK')
        self.assertFalse(result.success)
        self.assertIn("does not know the vendor id 'STK'", result.message)

    def test_a_library_that_does_not_exist_is_refused(self):
        result = self.run_it(999)
        self.assertFalse(result.success)
        self.assertIn('not in device.conf', result.message)

    def test_cartridges_beyond_the_empty_slots_are_refused_by_default(self):
        """Growing a library changes what it advertises to backup software."""
        empty = ConfigService(self.config).library_contents(10).summary()['empty_slots']
        before = set(self.drives())
        result = self.run_it(tapes=empty + 5)
        self.assertFalse(result.success)
        self.assertIn('empty slot', result.message)
        self.assertIn('ask for it explicitly', ' '.join(result.errors))
        self.assertEqual(set(self.drives()), before,
                         'the drive must not be added when the slots refuse')

    def test_expand_slots_grows_the_library_when_asked(self):
        empty = ConfigService(self.config).library_contents(10).summary()['empty_slots']
        result = self.run_it(tapes=empty + 2, expand_slots=True)
        self.assertTrue(result.success, result.message)
        grown = ConfigService(self.config).library_contents(10).summary()
        self.assertGreaterEqual(grown['empty_slots'], empty + 2)

    def test_the_gate_is_applied_to_the_revision_it_would_inherit(self):
        """Found by a dry run against library 50's real configuration.

        add() inherits a sibling's revision, and library 50's STK drives report
        '0016' - below the IBM LTO-8 minimum. Validating only what the caller
        passed meant the drive was written and the verify step then caught it,
        leaving an added drive LTFS refuses.
        """
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace(
            '#Product revision level: 2160\n', ' Product revision level: 0016\n'))
        before = set(self.drives())
        result = self.run_it()
        self.assertFalse(result.success)
        self.assertIn('HB81', self.steps(result)['validate']['message'])
        self.assertIn('would inherit', ' '.join(result.errors))
        self.assertEqual(set(self.drives()), before, 'nothing may be written')

    def test_a_revision_that_clears_the_gate_is_accepted(self):
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace(
            '#Product revision level: 2160\n', ' Product revision level: 0016\n'))
        result = self.run_it(revision='HB82')
        self.assertTrue(result.success, result.message)
        self.assertIn('rev HB82', self.steps(result)['verify']['message'])

    # -- what it does not do ------------------------------------------------

    def test_it_only_ever_adds(self):
        """No room is a refusal, not a removal: taking a drive away restarts a
        library and is an operator's decision, not a side effect of LTFS."""
        before = set(self.drives())
        self.run_it()
        after = set(self.drives())
        self.assertTrue(after > before, 'a drive should have been added')
        self.assertTrue(before <= after, 'no drive may be removed')
        source = Path(workflow_module.__file__).read_text()
        self.assertNotIn('.remove(', source)

    def test_it_never_formats_a_cartridge(self):
        """mkltfs erases; it stays a separate deliberate act."""
        source = Path(workflow_module.__file__).read_text()
        self.assertNotIn('format_cartridge', source)
        self.assertNotIn('LtfsService', source)

    def test_the_drive_is_added_without_its_own_restart(self):
        """One restart at the end, not one per config write - otherwise there is
        a window where the drive exists and the slots do not."""
        from apps.libraries.services.drives import DriveService
        with mock.patch.object(DriveService, 'add',
                               wraps=DriveService(self.config).add) as add:
            self.run_it()
        self.assertFalse(add.call_args.kwargs['restart'])


class LtfsMediaWorkflowTests(TestCase):
    """add_ltfs_media_workflow(): cartridges an LTFS drive can format.

    The drive comes first, always. A library with no LTFS-capable drive is
    refused rather than warned at, because creating LTO-8 cartridges for a
    library that cannot mount them leaves an operator believing they have
    finished when they have not.

    It creates BLANK media of a density LTFS can work with. It does not format:
    mkltfs erases a cartridge and is gated twice over on purpose.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in CONFIG_FILES:
            shutil.copy(FIXTURES / name, self.config)

        patches = [
            mock.patch.object(LibraryService, 'restart_services',
                              return_value=success_result('Services restarted')),
            mock.patch.object(workflow_module, '_record',
                              return_value=('record', True, 'recorded')),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])

    def run_it(self, library_id=10, count=2, **kwargs):
        from apps.libraries.services.libraries import add_ltfs_media_workflow

        kwargs.setdefault('config_directory', self.config)
        return add_ltfs_media_workflow(library_id, count, **kwargs)

    def steps(self, result):
        return {step['step']: step for step in result.data['steps']}

    def created(self, barcodes=('ABC001L8', 'ABC002L8'), write=True):
        """Stand in for create_bulk: its result shape AND its side effect.

        It puts the barcodes into library_contents, because that is what the
        real one does and because the verify step reads the file back. A fake
        that returns a success without writing anything is not modelling
        create_bulk, it is modelling a bug - and the first version of this
        helper did exactly that, which made two tests fail for the right
        reason.

        write=False is the modelling of that bug, on purpose, for the test that
        checks verification catches it.
        """
        def fake(library_id, count, **kwargs):
            if write:
                config = ConfigService(self.config)
                contents = config.library_contents(library_id)
                empty = [s for s in contents.slots if not s.full]
                for slot, barcode in zip(empty, barcodes):
                    slot.barcode = barcode
                config.write_library_contents(
                    library_id, contents_format.render(contents), backup=False)
            return success_result(
                f'Created {len(barcodes)} of {count} tapes',
                {'library_id': library_id,
                 'created': [{'barcode': b} for b in barcodes],
                 'requested': count, 'failed': []})

        return mock.patch.object(TapeService, 'create_bulk', side_effect=fake)

    # -- the drive comes first ---------------------------------------------

    def test_no_ltfs_capable_drive_is_refused_and_names_the_step_to_do_first(self):
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace(
            ' Vendor identification: IBM\n', ' Vendor identification: STK\n'))
        with self.created() as bulk:
            result = self.run_it()
        self.assertFalse(result.success)
        self.assertIn('no drive LTFS can open', result.message)
        self.assertIn('add one first', ' '.join(result.errors))
        bulk.assert_not_called()

    def test_the_drive_decides_the_density(self):
        """An ULT3580-TD8 writes LTO8 and LTO7; the newest comes first."""
        with self.created():
            result = self.run_it()
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['density'], 'LTO8')
        self.assertIn('ULT3580-TD8', self.steps(result)['validate']['message'])

    def test_a_density_too_old_to_partition_is_refused_with_the_reason(self):
        """LTO-5 introduced partitioning. An ULT3580-TD5 writes LTO-4 happily
        and no LTFS volume can live on it - so the drive has to be one that
        HANDLES the density, or the refusal is the wrong one."""
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace('ULT3580-TD8', 'ULT3580-TD5'))
        with self.created() as bulk:
            result = self.run_it(density='LTO4')
        self.assertFalse(result.success)
        self.assertIn('cannot be partitioned', ' '.join(result.errors))
        bulk.assert_not_called()

    def test_a_read_only_density_is_refused_because_mkltfs_writes(self):
        """An ULT3580-TD7 loads LTO5 read-only. The medium can be partitioned;
        this drive cannot write it."""
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace('ULT3580-TD8', 'ULT3580-TD7'))
        with self.created() as bulk:
            result = self.run_it(density='LTO5')
        self.assertFalse(result.success)
        self.assertIn('read-only', ' '.join(result.errors))
        self.assertIn('mkltfs writes', ' '.join(result.errors))
        bulk.assert_not_called()

    def test_a_density_the_drive_does_not_handle_at_all_is_refused(self):
        """Different refusal from the two above, and it says which."""
        with self.created() as bulk:
            result = self.run_it(density='LTO4')
        self.assertFalse(result.success)
        self.assertIn('does not handle LTO4', result.message)
        bulk.assert_not_called()

    def test_a_density_the_drive_does_not_handle_is_refused(self):
        with self.created() as bulk:
            result = self.run_it(density='LTO9')
        self.assertFalse(result.success)
        self.assertIn('does not handle LTO9', result.message)
        bulk.assert_not_called()

    def test_a_chosen_density_it_can_write_is_used(self):
        with self.created() as bulk:
            result = self.run_it(density='LTO7')
        self.assertTrue(result.success, result.message)
        self.assertEqual(bulk.call_args.kwargs['density'], 'LTO7')

    # -- slots -------------------------------------------------------------

    def test_more_cartridges_than_slots_is_refused_by_default(self):
        empty = ConfigService(self.config).library_contents(10).summary()['empty_slots']
        with self.created() as bulk:
            result = self.run_it(count=empty + 5)
        self.assertFalse(result.success)
        self.assertIn('empty slot', result.message)
        self.assertIn('ask for it explicitly', ' '.join(result.errors))
        bulk.assert_not_called()

    def test_expand_slots_grows_the_library_when_asked(self):
        empty = ConfigService(self.config).library_contents(10).summary()['empty_slots']
        with self.created():
            result = self.run_it(count=empty + 2, expand_slots=True)
        self.assertTrue(result.success, result.message)
        grown = ConfigService(self.config).library_contents(10).summary()
        self.assertGreaterEqual(grown['empty_slots'] + grown['full_slots'],
                                empty + 2)

    def test_zero_cartridges_is_refused(self):
        with self.created() as bulk:
            result = self.run_it(count=0)
        self.assertFalse(result.success)
        bulk.assert_not_called()

    def test_a_library_that_does_not_exist_is_refused(self):
        with self.created() as bulk:
            result = self.run_it(999)
        self.assertFalse(result.success)
        self.assertIn('not in device.conf', result.message)
        bulk.assert_not_called()

    # -- verification ------------------------------------------------------

    def test_verify_checks_the_barcodes_by_name(self):
        """Counting matching suffixes could not fail: library 60 already held
        five L7 cartridges, so "at least two exist" passed while nothing had
        been written."""
        with self.created(('NOPE001L8', 'NOPE002L8'), write=False):
            result = self.run_it()
        self.assertFalse(self.steps(result)['verify']['ok'])
        self.assertIn('not in library_contents',
                      self.steps(result)['verify']['message'])

    def test_a_creation_reporting_no_barcodes_fails_verification(self):
        """A check with no subject must not report success."""
        with mock.patch.object(TapeService, 'create_bulk',
                               return_value=success_result('made some',
                                                           {'created': []})):
            result = self.run_it()
        self.assertFalse(self.steps(result)['verify']['ok'])
        self.assertIn('nothing could be verified',
                      self.steps(result)['verify']['message'])

    def test_verify_says_these_are_not_ltfs_volumes_yet(self):
        """The difference between an operator who knows what they have and one
        who thinks the library is ready."""
        with self.created():
            result = self.run_it()
        message = self.steps(result)['verify']['message']
        self.assertIn('blank media', message)
        self.assertIn('not LTFS volumes', message)
        self.assertIn('mkltfs', message)

    # -- what it does not do -----------------------------------------------

    def test_it_never_formats_anything(self):
        source = Path(workflow_module.__file__).read_text()
        self.assertNotIn('format_cartridge', source)
        self.assertNotIn('LtfsService', source)

    def test_it_owns_no_barcode_logic(self):
        """create_bulk decides the barcodes, the suffix and the media files."""
        with self.created() as bulk:
            self.run_it()
        self.assertEqual(bulk.call_args.args[:2], (10, 2))
        self.assertEqual(set(bulk.call_args.kwargs), {'density'})

    def test_one_restart_after_every_write(self):
        with self.created():
            result = self.run_it(count=1)
        order = [s['step'] for s in result.data['steps']]
        self.assertEqual(order.index('restart'), order.index('media') + 1)
        self.assertEqual(order.count('restart'), 1)


class LtfsProvisioningReadTests(TestCase):
    """ltfs_provisioning(): what a library has, lacks, and could be given.

    Composition only. If a number here disagrees with a page elsewhere, this is
    the wrong place to fix it.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in CONFIG_FILES:
            shutil.copy(FIXTURES / name, self.config)

    def read(self, library_id=10):
        from apps.libraries.services.libraries import ltfs_provisioning

        return ltfs_provisioning(library_id, self.config)

    def make_incapable(self):
        """Relabel the fixture's IBM drives STK, which LTFS has no table for."""
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace(
            ' Vendor identification: IBM\n', ' Vendor identification: STK\n'))

    def test_a_library_that_does_not_exist_is_refused(self):
        self.assertFalse(self.read(999).success)

    def test_it_reports_both_slot_kinds(self):
        data = self.read().data
        self.assertIn('used', data['drive_slots'])
        self.assertIn('max', data['drive_slots'])
        self.assertIn('empty', data['storage_slots'])
        self.assertEqual(data['drive_slots']['used'], len(data['drives']))

    def test_a_capable_library_needs_nothing_when_it_has_ltfs_media(self):
        with mock.patch('apps.libraries.services.tapes.ltfs_state.state_for_all') as states:
            states.return_value = {'X': mock.Mock(state='ltfs')}
            data = self.read().data
        self.assertEqual(data['needs'], [])
        self.assertEqual(data['ltfs_media'], 1)

    def test_a_capable_library_with_no_ltfs_media_needs_media(self):
        with mock.patch('apps.libraries.services.tapes.ltfs_state.state_for_all',
                        return_value={}):
            data = self.read().data
        self.assertEqual(data['needs'], ['media'])
        self.assertTrue(data['can_add_media'])

    def test_an_incapable_library_needs_a_drive_and_media_is_not_claimed(self):
        """'media' is not in needs, because the cartridges were never read."""
        self.make_incapable()
        data = self.read().data
        self.assertEqual(data['needs'], ['drive'])
        self.assertIsNone(data['ltfs_media'])
        self.assertFalse(data['can_add_media'])
        self.assertIn('add a drive LTFS can open first',
                      data['cannot_add_media_because'])

    def test_no_capable_drive_means_no_cartridge_is_read(self):
        """The same gate as TapeService.list(). Library 30 has 40 cartridges
        and no capable drive; reading all 40 to print a number nobody can act
        on is what the gate exists to prevent."""
        from apps.libraries.services.tapes import media as tape_media

        self.make_incapable()
        with mock.patch.object(tape_media, 'read_media_files') as reader:
            self.read()
        reader.assert_not_called()

    def test_the_message_never_claims_a_count_it_did_not_read(self):
        self.make_incapable()
        result = self.read()
        self.assertIn('cartridges were not read', result.message)
        self.assertNotIn('0 LTFS', result.message)

    def test_drive_candidates_are_pairs_ltfs_accepts(self):
        from apps.libraries.services.profiles import ltfs_support

        for candidate in self.read().data['drive_candidates']:
            with self.subTest(**candidate):
                verdict = ltfs_support.supports(candidate['vendor'],
                                                candidate['model'])
                self.assertTrue(verdict.supported, verdict.reason)

    def test_a_candidate_carries_its_firmware_minimum_only_when_there_is_one(self):
        rows = {c['model']: c for c in self.read().data['drive_candidates']}
        self.assertEqual(rows['ULT3580-TD5']['firmware_minimum'], 'B170')
        self.assertEqual(rows['ULT3580-TD8']['firmware_minimum'], 'HB81')
        self.assertEqual(rows['ULT3580-TD7']['firmware_minimum'], '')

    def test_media_candidates_come_from_the_capable_drive(self):
        """The drive is added first, so there is always one to ask."""
        densities = [m['density'] for m in self.read().data['media_candidates']
                     if m['usable']]
        self.assertEqual(densities, ['LTO8', 'LTO7'])

    def test_media_candidates_are_empty_without_a_capable_drive(self):
        self.make_incapable()
        self.assertEqual(self.read().data['media_candidates'], [])

    def test_it_writes_nothing_and_starts_nothing(self):
        before = (self.config / 'device.conf').read_text()
        with mock.patch.object(LibraryService, 'restart_services') as restart:
            self.read()
        restart.assert_not_called()
        self.assertEqual((self.config / 'device.conf').read_text(), before)

    def test_every_drive_row_says_whether_ltfs_opens_it_and_why_not(self):
        self.make_incapable()
        for row in self.read().data['drives']:
            with self.subTest(drive=row['drive_id']):
                self.assertFalse(row['ltfs_capable'])
                self.assertIn('STK', row['ltfs_reason'])


class DaemonGuardTests(TestCase):
    """A workflow pointed at a copy must not touch the running daemons.

    They read /etc/mhvtl through their systemd units. Restarting them from a
    scratch directory applies a file they never read and interrupts libraries
    that are in use.

    This class exists because it happened. On 1 October 2026
    add_ltfs_media_workflow was run with --config-dir against a copy and
    restarted the live vtllibrary@60, which unloaded the cartridge from drive 0
    while an `ltfs` process still held the device. Nothing was lost. The cause
    was daemons_are_ours() comparing the copy against config_dir(), which
    --config-dir had already moved to the copy - it compared a directory with
    itself and said yes.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in CONFIG_FILES:
            shutil.copy(FIXTURES / name, self.config)

    def test_a_scratch_directory_is_not_the_daemons_directory(self):
        from apps.libraries.services.libraries.lifecycle import daemons_are_ours

        self.assertFalse(daemons_are_ours(self.config))

    def test_by_default_the_daemons_read_what_this_process_reads(self):
        """Right for the web app, whose setting IS the live directory, and for
        this suite, whose setting is a copy standing in for it."""
        from django.test import override_settings

        from apps.libraries.services.core import config_dir, daemon_config_dir

        with override_settings(MHVTL_CONFIG_DIR=str(self.config)):
            self.assertEqual(config_dir(), self.config)
            self.assertEqual(daemon_config_dir(), self.config)

    def test_pinning_the_daemons_directory_is_what_closes_the_hole(self):
        """The bug in one assertion. --config-dir moves config_dir() to the
        copy, so comparing against it compared a directory with itself and said
        yes. The CLI pins the daemons' directory, and then it says no."""
        from django.test import override_settings

        from apps.libraries.services.core import config_dir, daemon_config_dir
        from apps.libraries.services.libraries.lifecycle import daemons_are_ours

        with override_settings(MHVTL_CONFIG_DIR=str(self.config),
                               MHVTL_DAEMON_CONFIG_DIR='/etc/mhvtl'):
            self.assertEqual(config_dir(), self.config)
            self.assertEqual(daemon_config_dir(), Path('/etc/mhvtl'))
            self.assertFalse(daemons_are_ours(self.config))

    def test_the_cli_pins_it_whenever_config_dir_points_elsewhere(self):
        """Asserted on the CLI's own setup, so the guard is not left depending
        on a test remembering to pin it."""
        import inspect

        from mhvtl_cli import main

        source = inspect.getsource(main.setup_django)
        self.assertIn('MHVTL_DAEMON_CONFIG_DIR', source)

    def test_the_live_directory_is_the_daemons_directory(self):
        from apps.libraries.services.core import daemon_config_dir
        from apps.libraries.services.libraries.lifecycle import daemons_are_ours

        self.assertTrue(daemons_are_ours(daemon_config_dir()))

    def _steps(self, result):
        return {step['step']: step for step in result.data['steps']}

    def test_the_media_workflow_does_not_restart_from_a_copy(self):
        from apps.libraries.services.libraries import add_ltfs_media_workflow

        with mock.patch.object(LibraryService, 'restart_services') as restart, \
             mock.patch.object(TapeService, 'create_bulk',
                               return_value=success_result(
                                   'made', {'created': []})), \
             mock.patch.object(workflow_module, '_record',
                               return_value=('record', True, 'recorded')):
            result = add_ltfs_media_workflow(10, 1, config_directory=self.config)
        restart.assert_not_called()
        self.assertIn('NOT restarted', self._steps(result)['restart']['message'])

    def test_the_drive_workflow_does_not_restart_from_a_copy(self):
        from apps.libraries.services.libraries import add_ltfs_drive_workflow

        with mock.patch.object(LibraryService, 'restart_services') as restart, \
             mock.patch.object(workflow_module, '_create_ltfs_media',
                               return_value=('media', True, 'none asked for')), \
             mock.patch.object(workflow_module, '_record',
                               return_value=('record', True, 'recorded')):
            result = add_ltfs_drive_workflow(10, config_directory=self.config)
        restart.assert_not_called()
        self.assertIn('NOT restarted', self._steps(result)['restart']['message'])

    def test_the_create_workflow_does_not_restart_from_a_copy(self):
        """The same hole was in create_library_workflow, unnoticed."""
        from apps.libraries.services.libraries import create_library_workflow

        with mock.patch.object(LibraryService, 'validate',
                               return_value=success_result('ok', {'warnings': []})), \
             mock.patch.object(LibraryService, 'create',
                               return_value=success_result('made',
                                                           {'library_id': 40})), \
             mock.patch.object(LibraryService, 'restart_services') as restart, \
             mock.patch.object(LibraryService, 'recognised_by_mhvtl',
                               return_value=True), \
             mock.patch.object(workflow_module, '_create_media',
                               return_value=('media', True, 'made')), \
             mock.patch.object(workflow_module, '_record',
                               return_value=('record', True, 'recorded')):
            result = create_library_workflow({'profile': 'IBM', 'library_id': 40},
                                             config_directory=self.config)
        restart.assert_not_called()
        self.assertIn('NOT restarted', self._steps(result)['restart']['message'])


class NewDriveNeedsItsOwnDaemonTests(TestCase):
    """A new drive's vtltape@<id> has to be started, not just the library.

    Restarting a library restarts the robot - vtllibrary@<N> - and nothing
    else. A drive whose daemon was never started has no device node at all: it
    is in device.conf, `ltfs drives` reports that LTFS can open it, and there
    is nothing to open.

    Found by recording the LTFS video. `ltfs add-drive 80` reported six green
    steps and left a drive lsscsi had never heard of; `systemctl start
    vtltape@83` made /dev/sg34 appear. DriveService.add() had always started
    the unit, so the two paths disagreed.

    The workflow tests above run against a temp directory, where
    daemons_are_ours() is false and the whole restart step returns early -
    which is exactly why this went unnoticed. These exercise the live branch.
    """

    def _restart(self, **kwargs):
        service = mock.Mock()
        service.restart_services.return_value = success_result('Restarted')
        with mock.patch.object(workflow_module, 'daemons_are_ours',
                               create=True, return_value=True), \
             mock.patch('apps.libraries.services.libraries.lifecycle'
                        '.daemons_are_ours', return_value=True), \
             mock.patch('apps.libraries.services.console.units.start_library',
                        return_value={}) as start:
            step = workflow_module._restart_step(
                80, None, service, what='The configuration is', **kwargs)
        return step, start, service

    def test_a_new_drive_s_unit_is_started(self):
        step, start, _ = self._restart(start_drive=83)
        start.assert_called_once_with(80, [83])
        self.assertTrue(step[1], step[2])

    def test_the_library_is_still_restarted_after_it(self):
        _, _, service = self._restart(start_drive=83)
        service.restart_services.assert_called_once_with(80)

    def test_without_a_new_drive_nothing_extra_is_started(self):
        """Creating a library already starts its units; only an ADDED drive
        needs this."""
        _, start, service = self._restart()
        start.assert_not_called()
        service.restart_services.assert_called_once_with(80)

    def test_a_unit_that_will_not_start_is_a_failure_and_says_why(self):
        service = mock.Mock()
        service.restart_services.return_value = success_result('Restarted')
        with mock.patch('apps.libraries.services.libraries.lifecycle'
                        '.daemons_are_ours', return_value=True), \
             mock.patch('apps.libraries.services.console.units.start_library',
                        return_value={'vtltape@83.service': False}):
            name, ok, detail = workflow_module._restart_step(
                80, None, service, what='The configuration is', start_drive=83)
        self.assertFalse(ok)
        self.assertIn('vtltape@83.service', detail)
        self.assertIn('no device until it does', detail)
        service.restart_services.assert_not_called()

    def test_a_scratch_directory_still_starts_nothing(self):
        """The guard comes first: a workflow pointed somewhere else must not
        touch the running daemons, drive unit included."""
        service = mock.Mock()
        with mock.patch('apps.libraries.services.libraries.lifecycle'
                        '.daemons_are_ours', return_value=False), \
             mock.patch('apps.libraries.services.console.units.start_library') as start:
            name, ok, detail = workflow_module._restart_step(
                80, '/tmp/scratch', service, what='The configuration is',
                start_drive=83)
        start.assert_not_called()
        service.restart_services.assert_not_called()
        self.assertTrue(ok)
        self.assertIn('NOT restarted', detail)
