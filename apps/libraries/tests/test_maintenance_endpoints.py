"""The maintenance endpoints in ajax_views: regenerate, validate, availability,
library status, preview and export.

They went through adapters/mhvtl_script_service.py, which once shelled out to
MHVTL's own /usr/bin scripts; they now call services directly, and these tests
moved with them. The one that matters most is RegenerateTests: the original
ran `generate_device_conf --force`, which writes MHVTL's demo configuration
over whatever device.conf holds, from a POST endpoint.
"""
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import RequestFactory, TestCase

from apps.libraries import ajax_views
from apps.libraries.services.config.service import ConfigService
from apps.libraries.services.console import system
from apps.libraries.services.core import failure_result, success_result
from apps.libraries.services.libraries import LibraryService, lifecycle

FIXTURES = Path(__file__).parent / 'fixtures'


def get(view, path, **kwargs):
    request = RequestFactory().get(path)
    request.session = {'mhvtl_logged_in': True}
    return json.loads(view(request, **kwargs).content)


class RegenerateTests(TestCase):
    """`generate_device_conf --force` is not a maintenance action."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.service = ConfigService(self.config)

    def test_device_conf_is_not_touched(self):
        """One click used to replace an operator's libraries with the demo set."""
        before = (self.config / 'device.conf').read_text()
        result = self.service.regenerate_library_contents(force=True)
        self.assertTrue(result.success, result.message)
        self.assertEqual((self.config / 'device.conf').read_text(), before)

    def test_no_mhvtl_script_is_run(self):
        from apps.libraries.services.core import shell
        with mock.patch.object(shell, 'run') as run, \
                mock.patch.object(shell, 'sudo') as sudo:
            self.service.regenerate_library_contents(force=True)
        for call in list(run.call_args_list) + list(sudo.call_args_list):
            self.assertNotIn('generate_device_conf', ' '.join(map(str, call[0][0])))

    def test_the_contents_files_are_written(self):
        self.assertTrue(self.service.regenerate_library_contents(force=True).success)
        for library_id in (10, 20, 30):
            self.assertTrue((self.config / f'library_contents.{library_id}').exists())

    def test_a_failure_comes_back_as_a_result_not_an_exception(self):
        result = ConfigService(tempfile.mkdtemp()).regenerate_library_contents(force=True)
        self.assertFalse(result.success)
        self.assertTrue(result.message)


class ValidateTests(TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10', 'library_contents.20',
                     'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.media = tempfile.mkdtemp()

    def check(self, config=None):
        return ajax_views._config_check(config or self.config, self.media)

    def test_a_consistent_configuration_passes(self):
        valid, issues = self.check()
        self.assertTrue(valid, issues)

    def test_a_missing_media_directory_is_an_issue(self):
        valid, issues = ajax_views._config_check(self.config, '/nonexistent/media')
        self.assertFalse(valid)
        self.assertIn('/nonexistent/media', issues[0])

    def test_a_missing_contents_file_is_an_issue(self):
        """The original check looked at device.conf only, so this passed."""
        (self.config / 'library_contents.20').unlink()
        valid, issues = self.check()
        self.assertFalse(valid)
        self.assertTrue(any('library_contents.20' in issue for issue in issues))

    def test_a_drive_whose_library_is_gone_is_an_issue(self):
        text = (self.config / 'device.conf').read_text()
        (self.config / 'device.conf').write_text(
            text + '\nDrive: 91 CHANNEL: 00 TARGET: 40 LUN: 00\n'
                   ' Library ID: 90 Slot: 01\n')
        valid, issues = self.check()
        self.assertFalse(valid)
        self.assertTrue(any('91' in issue for issue in issues))

    def test_an_unreadable_device_conf_is_an_issue(self):
        valid, issues = self.check(Path(tempfile.mkdtemp()))
        self.assertFalse(valid)
        self.assertTrue(issues)


class AvailabilityTests(TestCase):
    """Installed is not the same as running (console/system.mhvtl_installation)."""

    def _state(self, backend='mhvtl', target_active=True):
        from apps.libraries.services.console import modules, units

        status = mock.MagicMock()
        status.target_active = target_active
        return (mock.patch.object(modules, 'summary',
                                  return_value={'backend': backend}),
                mock.patch.object(units, 'status', return_value=status))

    def installation(self, **state):
        modules_patch, units_patch = self._state(**state)
        with modules_patch, units_patch:
            return system.mhvtl_installation()

    def test_reports_each_program_the_app_runs(self):
        status = self.installation()
        self.assertEqual(set(status['scripts']), set(system.REQUIRED_PROGRAMS))
        self.assertIn('vtlcmd', status['scripts'])

    def test_a_missing_program_makes_it_unavailable(self):
        modules_patch, units_patch = self._state()
        with modules_patch, units_patch:
            status = system.mhvtl_installation(bin_dir=tempfile.mkdtemp())
        self.assertFalse(status['available'])
        self.assertTrue(any('not installed' in e for e in status['errors']))

    def test_an_unloaded_module_makes_it_unavailable(self):
        """Every program installed and still no library: the case the original
        check, which looked only at files, called available."""
        status = self.installation(backend='none')
        self.assertFalse(status['available'])
        self.assertTrue(any('backend' in error for error in status['errors']))

    def test_the_tcmu_backend_counts_as_loaded(self):
        """1.8 can run on TCMU instead of mhvtl.ko."""
        self.assertTrue(self.installation(backend='tcmu')['module_loaded'])

    def test_a_stopped_target_is_reported_without_hiding_the_rest(self):
        status = self.installation(target_active=False)
        self.assertFalse(status['service_running'])
        self.assertTrue(any('mhvtl.target' in error for error in status['errors']))

    def test_an_unreadable_running_state_does_not_raise(self):
        from apps.libraries.services.console import modules
        with mock.patch.object(modules, 'summary', side_effect=OSError('no lsmod')):
            status = system.mhvtl_installation()
        self.assertTrue(any('running state' in error for error in status['errors']))


class LibraryStatusEndpointTests(TestCase):
    """`vtlcmd -l <id> status` is not a vtlcmd invocation; mtx answers."""

    def _status(self, result):
        from apps.libraries.services.operations.service import OperationsService
        return mock.patch.object(OperationsService, 'status', return_value=result)

    def test_a_working_library_reports_its_slots(self):
        with self._status(success_result('ok', {'slots': [], 'drives': []})):
            answer = get(ajax_views.get_library_status_mhvtl_ajax, '/', library_id=10)
        self.assertTrue(answer['success'])
        self.assertEqual(answer['library_id'], 10)
        self.assertIn('slots', answer['status'])

    def test_an_offline_library_reports_why(self):
        with self._status(failure_result('Library 10 did not answer', ['offline'])):
            answer = get(ajax_views.get_library_status_mhvtl_ajax, '/', library_id=10)
        self.assertFalse(answer['success'])
        self.assertIn('did not answer', answer['error'])


class PreviewTests(TestCase):
    """What create() would write, before anything is written."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def preview(self, spec):
        return lifecycle.preview(spec, self.config)

    def test_the_preview_is_valid_device_conf(self):
        from apps.libraries.services.config import device_conf

        result = self.preview({'library_id': 40, 'profile': 'IBM', 'num_drives': 2})
        self.assertTrue(result.success, result.message)
        parsed = device_conf.parse(result.data['text'])
        self.assertIn(40, parsed.libraries)
        self.assertEqual(sorted(parsed.drives_of(40)), [41, 42])
        self.assertEqual(result.data['drive_ids'], [41, 42])

    def test_nothing_is_written(self):
        before = (self.config / 'device.conf').read_text()
        self.preview({'library_id': 40, 'profile': 'IBM'})
        self.assertEqual((self.config / 'device.conf').read_text(), before)

    def test_the_preview_shows_the_targets_and_ids_create_would_use(self):
        from apps.libraries.services.config import device_conf, ids

        existing = device_conf.parse((self.config / 'device.conf').read_text())
        planned = ids.plan_library(existing, 40, 2)
        preview = device_conf.parse(
            self.preview({'library_id': 40, 'profile': 'IBM', 'num_drives': 2}).data['text'])
        self.assertFalse(preview.used_targets() & existing.used_targets())
        self.assertEqual(preview.libraries[40]['target'], planned[0])
        self.assertEqual(sorted(preview.drives_of(40)), planned[1])

    def test_a_bad_specification_is_a_failure_with_the_reason(self):
        result = self.preview({'library_id': 40, 'profile': 'acme'})
        self.assertFalse(result.success)
        self.assertIn('Cannot preview', result.message)

    def test_validation_problems_come_back_beside_the_text(self):
        """Library 10 exists; the preview still shows, with the reason.

        Errors and warnings come back apart, so a caller can tell a refusal
        from a grumble; an existing id is a refusal.
        """
        result = self.preview({'library_id': 10, 'profile': 'IBM'})
        problems = result.data['errors'] + result.data['warnings']
        self.assertTrue(any('10' in problem for problem in problems))
        self.assertTrue(any('10' in error for error in result.data['errors']))

    def test_an_existing_library_is_shown_as_written(self):
        result = LibraryService(self.config).config_text(10)
        self.assertTrue(result.success, result.message)
        text = result.data['text']
        self.assertTrue(text.startswith('Library: 10 '))
        for drive_id in (11, 12, 13, 14):
            self.assertIn(f'Drive: {drive_id} ', text)
        self.assertNotIn('Library: 30', text)
        self.assertFalse(LibraryService(self.config).config_text(99).success)


class ExportSpecTests(TestCase):
    """export and preview built a specification without a profile, which the
    library rules refuse; _spec_for takes it from the brand."""

    def test_the_profile_comes_from_the_brand(self):
        library = mock.MagicMock(library_id=40, unit_serial_number='SER40')
        library.brand.name = 'Ibm'
        library.model.name = '03584L32'
        spec = ajax_views._spec_for(library)
        self.assertEqual(spec['profile'], 'IBM')
        self.assertEqual(spec['library_model'], '03584L32')
        self.assertEqual(spec['serial'], 'SER40')

    def test_a_refused_create_comes_back_with_its_reasons(self):
        config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', config)
        result = LibraryService(config).create({'library_id': 10, 'profile': 'IBM'},
                                               start_services=False)
        self.assertFalse(result.success)
        self.assertIn('already exists', ' '.join(result.errors))
