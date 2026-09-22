"""End-to-end verification: does a library hold what is written to it?

Step 8 of the service-layer refactor. tar, mt and the robot are faked; the data
generation and checksumming are real, against a scratch directory, because they
are cheap and the point of the module is that they catch a corrupted byte.

The test that matters most is StepOrderTests: the unmount between writing and
reading is what makes this a verification rather than a file copy, and a run
without it passes on a drive that is writing to nothing.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.verification import (StepResult,
                                                   VerificationReport,
                                                   VerificationService)
from apps.libraries.services.verification import data as test_data
from apps.libraries.services.verification import tape_io, workflow

FIXTURES = Path(__file__).parent / 'fixtures'


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


def failed(stderr='no'):
    return CommandResult(['fake'], 1, '', stderr)


class DataGenerationTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_writes_the_requested_number_of_files(self):
        files = test_data.generate(self.dir, size_mb=2, file_count=4)
        self.assertEqual(len(files), 4)
        self.assertEqual(len(list(self.dir.glob('*.dat'))), 4)

    def test_the_total_is_about_the_requested_size(self):
        test_data.generate(self.dir, size_mb=2, file_count=2)
        total = sum(p.stat().st_size for p in self.dir.glob('*.dat'))
        self.assertAlmostEqual(total / (1024 * 1024), 2, delta=0.1)

    def test_the_data_is_not_compressible(self):
        """MHVTL enables compression by default, so a file of zeros would
        verify that compression works and say nothing about the tape."""
        import zlib
        test_data.generate(self.dir, size_mb=1, file_count=1)
        raw = next(self.dir.glob('*.dat')).read_bytes()
        self.assertGreater(len(zlib.compress(raw)), len(raw) * 0.9)

    def test_a_single_file_is_allowed(self):
        self.assertEqual(len(test_data.generate(self.dir, size_mb=1,
                                                file_count=1)), 1)


class ChecksumTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        test_data.generate(self.dir, size_mb=1, file_count=3)

    def test_checksums_every_file(self):
        found = test_data.checksums(self.dir)
        self.assertEqual(len(found), 3)
        self.assertTrue(all(len(entry['sha256']) == 64 for entry in found.values()))

    def test_writes_a_manifest_beside_the_data(self):
        """It travels with the data, so a restore can be checked later."""
        test_data.checksums(self.dir)
        self.assertTrue((self.dir / test_data.MANIFEST_NAME).exists())

    def test_the_manifest_does_not_checksum_itself(self):
        test_data.checksums(self.dir)
        found = test_data.checksums(self.dir)
        self.assertNotIn(test_data.MANIFEST_NAME, found)

    def test_identical_data_verifies(self):
        expected = test_data.checksums(self.dir)
        verified, corrupted, missing = test_data.verify(self.dir, expected)
        self.assertEqual(len(verified), 3)
        self.assertEqual((corrupted, missing), ([], []))

    def test_one_changed_byte_is_caught(self):
        """The whole point: size and mtime survive a corrupted restore."""
        expected = test_data.checksums(self.dir)
        victim = sorted(self.dir.glob('*.dat'))[0]
        raw = bytearray(victim.read_bytes())
        raw[0] ^= 0xFF
        victim.write_bytes(bytes(raw))

        verified, corrupted, missing = test_data.verify(self.dir, expected)
        self.assertEqual(len(corrupted), 1)
        self.assertEqual(corrupted[0]['filename'], victim.name)
        self.assertEqual(missing, [])

    def test_a_missing_file_is_reported_apart_from_a_corrupted_one(self):
        """Different discoveries: a tape that held less, or held it wrongly."""
        expected = test_data.checksums(self.dir)
        sorted(self.dir.glob('*.dat'))[0].unlink()

        verified, corrupted, missing = test_data.verify(self.dir, expected)
        self.assertEqual(len(missing), 1)
        self.assertEqual(corrupted, [])


class TapeDeviceTests(TestCase):
    def test_prefers_the_non_rewinding_node(self):
        """/dev/st0 rewinds on close, so a second archive overwrites the first."""
        with mock.patch.object(Path, 'exists', return_value=True):
            self.assertEqual(tape_io.writable_device('/dev/st0'), '/dev/nst0')

    def test_falls_back_when_there_is_no_non_rewinding_node(self):
        with mock.patch.object(Path, 'exists', return_value=False):
            self.assertEqual(tape_io.writable_device('/dev/st0'), '/dev/st0')

    def test_a_generic_node_is_left_alone(self):
        self.assertEqual(tape_io.writable_device('/dev/sg6'), '/dev/sg6')

    def test_writing_rewinds_first(self):
        calls = []

        def sudo(argv, **kwargs):
            calls.append(argv[0])
            return ok()

        with mock.patch.object(tape_io.shell, 'sudo', side_effect=sudo):
            tape_io.write_archive('/dev/nst0', Path('/tmp/x/source'))
        self.assertEqual(calls, ['mt', 'tar'])

    def test_the_archive_is_written_from_the_parent_directory(self):
        """Absolute paths in an archive will not extract without a flag, and an
        archive that will not extract is not a verified backup."""
        with mock.patch.object(tape_io.shell, 'sudo', return_value=ok()) as sudo:
            tape_io.write_archive('/dev/nst0', Path('/tmp/run/source'))
        argv = sudo.call_args[0][0]
        self.assertEqual(argv[:2], ['tar', '-cvf'])
        self.assertIn('-C', argv)
        self.assertEqual(argv[argv.index('-C') + 1], '/tmp/run')
        self.assertEqual(argv[-1], 'source')

    def test_reading_rewinds_first(self):
        """A tape at end-of-data extracts nothing, and tar calls that a
        malformed archive rather than an empty one."""
        calls = []

        def sudo(argv, **kwargs):
            calls.append(argv[0])
            return ok()

        target = Path(tempfile.mkdtemp())
        with mock.patch.object(tape_io.shell, 'sudo', side_effect=sudo):
            tape_io.read_archive('/dev/nst0', target)
        self.assertEqual(calls[:2], ['mt', 'tar'])

    def test_reading_takes_ownership_back(self):
        """tar writes as root and the checksum step reads as the web user."""
        calls = []

        def sudo(argv, **kwargs):
            calls.append(argv[0])
            return ok()

        with mock.patch.object(tape_io.shell, 'sudo', side_effect=sudo):
            tape_io.read_archive('/dev/nst0', Path(tempfile.mkdtemp()))
        self.assertIn('chown', calls)

    def test_a_failed_extract_does_not_chown(self):
        def sudo(argv, **kwargs):
            return ok() if argv[0] == 'mt' else failed('not a tar archive')

        with mock.patch.object(tape_io.shell, 'sudo', side_effect=sudo) as sudo_mock:
            result = tape_io.read_archive('/dev/nst0', Path(tempfile.mkdtemp()))
        self.assertFalse(result.ok)
        self.assertNotIn('chown', [c[0][0][0] for c in sudo_mock.call_args_list])

    def test_the_extracted_root_is_the_directory_tar_created(self):
        """An extract into restore/ produces restore/source/; returning the
        wrapper would make every file look missing."""
        target = Path(tempfile.mkdtemp())
        (target / 'source').mkdir()
        self.assertEqual(tape_io.extracted_root(target), target / 'source')

    def test_an_unexpected_layout_falls_back_to_the_target(self):
        target = Path(tempfile.mkdtemp())
        (target / 'one').mkdir()
        (target / 'two').mkdir()
        self.assertEqual(tape_io.extracted_root(target), target)


class DriveNumberTests(TestCase):
    """mtx numbers drives from 0 within a library; device.conf numbers globally."""

    def setUp(self):
        import shutil
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def test_the_first_drive_of_a_library_is_mtx_drive_zero(self):
        self.assertEqual(workflow._mtx_drive_number(20, 21, self.config), 0)

    def test_the_third_drive_is_mtx_drive_two(self):
        self.assertEqual(workflow._mtx_drive_number(20, 23, self.config), 2)

    def test_a_drive_of_another_library_is_not_found(self):
        self.assertIsNone(workflow._mtx_drive_number(20, 11, self.config))

    def test_position_is_used_rather_than_arithmetic(self):
        """drive_id - library_id is the same answer only while no drive has
        ever been removed from the library."""
        text = (self.config / 'device.conf').read_text()
        self.assertIn('Drive: 22', text)
        self.assertEqual(workflow._mtx_drive_number(20, 24, self.config), 3)


class StepOrderTests(TestCase):
    """The nine steps, stopping at the first failure, and putting the tape back."""

    def setUp(self):
        import shutil
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.work = Path(tempfile.mkdtemp())

        self.calls = []
        self.loaded = True              # what mtx says the drive holds
        patches = [
            mock.patch.object(workflow, '_device_for', return_value='/dev/nst0'),
            mock.patch.object(workflow.mapping, 'device_for_library',
                              return_value='/dev/sg4'),
            mock.patch.object(workflow.mtx, 'status',
                              side_effect=lambda device: self._state()),
            mock.patch.object(tape_io, 'write_archive',
                              side_effect=self._record('write', ok())),
            mock.patch.object(tape_io, 'read_archive',
                              side_effect=self._record('read', ok())),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _state(self):
        """An mtx status whose drive 0 holds a tape, or does not."""
        state = mock.MagicMock()
        state.drive.return_value = mock.MagicMock(full=self.loaded)
        return state

    def _record(self, name, result):
        def recorder(*args, **kwargs):
            self.calls.append(name)
            return result
        return recorder

    def _operations(self, mount_ok=True, unmount_ok=True):
        from apps.libraries.services.core import failure_result, success_result
        from apps.libraries.services.operations.service import OperationsService

        def mount(library_id, slot, drive):
            self.calls.append('mount')
            return (success_result('mounted') if mount_ok
                    else failure_result('busy', ['drive is loaded']))

        def unmount(library_id, drive, slot=None):
            self.calls.append('unmount')
            return (success_result('unmounted') if unmount_ok
                    else failure_result('stuck', ['robot refused']))

        return (mock.patch.object(OperationsService, 'mount', side_effect=mount),
                mock.patch.object(OperationsService, 'unmount', side_effect=unmount))

    def _run(self, **kwargs):
        mount, unmount = self._operations(**kwargs)
        with mount, unmount:
            return workflow.run(20, 1, 21, size_mb=1, file_count=2,
                                work_dir=self.work, cleanup_on_success=False,
                                config_dir=self.config)

    def test_the_tape_is_unmounted_between_writing_and_reading(self):
        """Without it, the read can come from a buffer that still holds what
        was just written - the test passes on a drive writing to nothing."""
        self._run()
        self.assertEqual(self.calls, ['mount', 'write', 'unmount', 'mount',
                                      'read', 'unmount'])

    def test_every_step_is_reported_in_order(self):
        report = self._run()
        self.assertEqual([step.step for step in report.steps],
                         ['generate data', 'checksum data', 'mount tape',
                          'write archive', 'unmount tape', 'mount tape again',
                          'read archive', 'verify checksums',
                          'return the tape'])

    def test_the_tape_goes_back_to_its_slot(self):
        """A verification leaves the library as it found it. It used to leave
        the cartridge in the drive, and the library could not then be deleted
        ("1 drive(s) still have tapes loaded")."""
        report = self._run()
        self.assertEqual(self.calls[-1], 'unmount')
        self.assertEqual(report.steps[-1].step, 'return the tape')
        self.assertTrue(report.steps[-1].success, report.steps[-1].message)

    def test_the_tape_goes_back_even_when_the_run_failed(self):
        report = self._run(unmount_ok=False)
        self.assertFalse(report.success)
        self.assertEqual(report.steps[-1].step, 'return the tape')

    def test_an_empty_drive_needs_no_unloading(self):
        self.loaded = False
        report = self._run()
        self.assertIn('already empty', report.steps[-1].message)
        self.assertEqual(self.calls.count('unmount'), 1)     # only the middle one

    def test_a_failed_mount_stops_the_run(self):
        """Later steps fail because this one did; continuing buries the cause."""
        report = self._run(mount_ok=False)
        self.assertFalse(report.success)
        self.assertEqual(report.failed_step.step, 'mount tape')
        self.assertNotIn('write', self.calls)

    def test_a_failed_unmount_stops_before_the_second_mount(self):
        report = self._run(unmount_ok=False)
        self.assertEqual(report.failed_step.step, 'unmount tape')
        self.assertNotIn('read', self.calls)

    def test_an_unknown_drive_is_refused_before_anything_is_written(self):
        report = workflow.run(20, 1, 99, work_dir=self.work,
                              config_dir=self.config)
        self.assertFalse(report.success)
        self.assertEqual(report.failed_step.step, 'resolve drive')
        self.assertEqual(self.calls, [])

    def test_a_failed_run_keeps_its_working_directory(self):
        """The restored files are the evidence for why it failed."""
        mount, unmount = self._operations(mount_ok=False)
        with mount, unmount:
            report = workflow.run(20, 1, 21, size_mb=1, file_count=1,
                                  work_dir=self.work, cleanup_on_success=True,
                                  config_dir=self.config)
        self.assertTrue(Path(report.summary['work_dir']).exists())

    def test_each_step_records_how_long_it_took(self):
        report = self._run()
        self.assertTrue(all(step.duration_seconds >= 0 for step in report.steps))
        self.assertGreater(report.duration_seconds, 0)


class ReportTests(TestCase):
    def test_an_empty_run_is_not_a_pass(self):
        self.assertFalse(VerificationReport(run_id='x', started_at='now').success)

    def test_the_first_failure_is_the_one_reported(self):
        report = VerificationReport(run_id='x', started_at='now')
        report.add(StepResult(step='one', success=True, message=''))
        report.add(StepResult(step='two', success=False, message='broke'))
        report.add(StepResult(step='three', success=False, message='and so did this'))
        self.assertEqual(report.failed_step.step, 'two')


class ServiceTests(TestCase):
    def test_a_passing_run_says_how_long_it_took(self):
        report = VerificationReport(run_id='abc', started_at='now')
        report.add(StepResult(step='verify checksums', success=True,
                              message='2 of 2 verified', duration_seconds=41.0))
        with mock.patch.object(workflow, 'run', return_value=report):
            result = VerificationService().run(10, 1, 11)

        self.assertTrue(result.success)
        self.assertIn('41s', result.message)

    def test_a_failing_run_names_the_step_and_where_the_evidence_is(self):
        report = VerificationReport(run_id='abc', started_at='now')
        report.add(StepResult(step='read archive', success=False,
                              message='tar failed reading from the tape',
                              errors=['not a tar archive']))
        report.summary = {'work_dir': '/tmp/mhvtl-verification/abc'}

        with mock.patch.object(workflow, 'run', return_value=report):
            result = VerificationService().run(10, 1, 11)

        self.assertFalse(result.success)
        self.assertIn('read archive', result.message)
        self.assertTrue(any('/tmp/mhvtl-verification/abc' in e
                            for e in result.errors))


class MissingExtractTests(TestCase):
    """tar can report success and produce nothing."""

    def test_a_directory_that_does_not_exist_is_not_an_exception(self):
        """It should report every file missing, which is the truth, rather than
        raising inside the step and reporting the traceback instead."""
        missing = Path(tempfile.mkdtemp()) / 'never-created'
        self.assertEqual(tape_io.extracted_root(missing), missing)

    def test_an_empty_extract_verifies_as_all_missing(self):
        empty = Path(tempfile.mkdtemp())
        verified, corrupted, missing = test_data.verify(
            empty, {'a.dat': {'sha256': 'x'}, 'b.dat': {'sha256': 'y'}})
        self.assertEqual(verified, [])
        self.assertEqual(sorted(missing), ['a.dat', 'b.dat'])


class VerificationViewTests(TestCase):
    """backup_restore_test_views.py calls services/verification directly.

    The helpers there replaced adapters/backup_restore_test_service.py: they
    turn the form's drive index into a device.conf drive id and the report
    into the shape the templates read. Nothing here runs a real test.
    """

    FIXTURES = Path(__file__).parent / 'fixtures'

    def setUp(self):
        from apps.libraries.services.config import device_conf
        self.conf = device_conf.parse((self.FIXTURES / 'device.conf').read_text())

    def _views(self):
        from apps.libraries import backup_restore_test_views as views
        return views

    def _with_conf(self):
        views = self._views()
        patch = mock.patch.object(views.ConfigService, 'device_conf',
                                  return_value=self.conf)
        patch.start()
        self.addCleanup(patch.stop)
        return views

    def _report(self, success=True):
        from apps.libraries.services.verification import VerificationReport
        report = VerificationReport(run_id='abc12345', started_at='t0',
                                    finished_at='t1')
        report.add(StepResult(step='generate', success=True, message='ok',
                              duration_seconds=1.234))
        report.add(StepResult(step='backup', success=success,
                              message='ok' if success else 'tar failed',
                              errors=[] if success else ['exit 2']))
        report.summary = {'library_id': 10, 'slot': 5, 'drive_id': 12,
                          'drive_number': 1, 'size_mb': 10, 'file_count': 5,
                          'work_dir': '/tmp/x'}
        return report

    def test_the_drive_index_is_resolved_against_device_conf(self):
        views = self._with_conf()
        self.assertEqual(views._drive_ids(10), [11, 12, 13, 14])
        with mock.patch.object(views.VerificationService, 'report',
                               return_value=self._report()) as run:
            views._run_test(10, 5, 1, size_mb=10, num_files=5)
        self.assertEqual(run.call_args.args, (10, 5, 12))
        self.assertEqual(run.call_args.kwargs['file_count'], 5)

    def test_an_index_past_the_drives_runs_nothing(self):
        views = self._with_conf()
        with mock.patch.object(views.VerificationService, 'report') as run:
            result = views._run_test(10, 5, 4, size_mb=10, num_files=5)
        run.assert_not_called()
        self.assertFalse(result['success'])
        self.assertIn('no drive at index 4', result['message'])

    def test_the_result_has_the_keys_the_templates_read(self):
        """summary.drive, num_files and the step counts were shown by the
        results template and never filled in."""
        views = self._with_conf()
        with mock.patch.object(views.VerificationService, 'report',
                               return_value=self._report(success=False)):
            result = views._run_test(10, 5, 1, size_mb=10, num_files=5)
        self.assertFalse(result['success'])
        self.assertEqual(result['message'], 'Failed at "backup": tar failed')
        self.assertEqual(result['test_id'], 'abc12345')
        self.assertEqual(result['steps'][0]['duration'], 1.23)
        self.assertEqual(result['steps'][1]['errors'], ['exit 2'])
        summary = result['summary']
        self.assertEqual((summary['drive'], summary['num_files']), (12, 5))
        self.assertEqual((summary['steps_passed'], summary['steps_completed']), (1, 2))
        import json
        json.dumps(result)          # it goes into the session

    def test_the_tape_list_leaves_out_cleaning_cartridges(self):
        views = self._views()
        from apps.libraries.services.core import success_result
        tapes = {'tapes': [{'slot': 1, 'barcode': 'E01001L8'},
                           {'slot': 2, 'barcode': 'CLN101L8'},
                           {'slot': None, 'barcode': 'E01003L8'}]}
        with mock.patch.object(views.TapeService, 'list',
                               return_value=success_result('ok', tapes)):
            self.assertEqual(views._test_tapes(10), [{'slot': 1, 'barcode': 'E01001L8'}])

    def test_the_tapes_endpoint_counts_drives_from_device_conf(self):
        import json
        from django.test import RequestFactory
        from apps.libraries.services.core import success_result
        views = self._with_conf()
        request = RequestFactory().get('/')
        request.session = {'mhvtl_logged_in': True}
        with mock.patch.object(views.TapeService, 'list',
                               return_value=success_result('ok', {'tapes': []})):
            response = views.get_library_tapes_ajax(request, 30)
        body = json.loads(response.content)
        self.assertTrue(body['success'])
        self.assertEqual(len(body['drives']), 4)
