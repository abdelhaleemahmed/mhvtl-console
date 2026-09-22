"""Library listing and the create workflow.

Step 6 of the service-layer refactor. The workflow tests matter most: that
sequence lived inside a view, interleaved with messages.* calls, which is why
there was no way to create a library except by submitting the form.

Creation is mocked here. Making a real library writes device.conf, restarts
every daemon and creates media files; that is verified once by hand against a
scratch configuration, not on every test run.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core import ServiceResult, failure_result, success_result
from apps.libraries.services.libraries import (LibraryInfo, LibraryService,
                                                create_library_workflow)
from apps.libraries.services.libraries import service as service_module
from apps.libraries.services.libraries import workflow as workflow_module

FIXTURES = Path(__file__).parent / 'fixtures'


class LibraryListingTests(TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10', 'library_contents.20',
                     'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.service = LibraryService(self.config)

    def test_lists_every_library(self):
        result = self.service.list()
        self.assertTrue(result.success)
        self.assertEqual(result.data['count'], 3)

    def test_sorts_by_id_even_though_the_file_does_not(self):
        """device.conf lists 10, 30, 20; a listing should not surprise anyone."""
        ids = [lib['library_id'] for lib in self.service.list().data['libraries']]
        self.assertEqual(ids, [10, 20, 30])

    def test_reports_model_drives_and_tapes(self):
        library = next(lib for lib in self.service.list().data['libraries']
                       if lib['library_id'] == 20)
        self.assertEqual(library['model'], 'SONY LIB-302')
        self.assertEqual(library['drives'], 4)
        self.assertEqual(library['tape_count'], 25)
        self.assertEqual(library['slot_count'], 40)

    def test_skipping_contents_avoids_reading_them(self):
        library = next(lib for lib in
                       self.service.list(with_contents=False).data['libraries']
                       if lib['library_id'] == 20)
        self.assertIsNone(library['tape_count'])

    def test_get_one_library(self):
        result = self.service.get(10)
        self.assertTrue(result.success)
        self.assertEqual(result.data['library']['drive_ids'], [11, 12, 13, 14])

    def test_unknown_library_is_a_failure_not_an_exception(self):
        self.assertFalse(self.service.get(99).success)

    def test_next_id_steps_by_ten(self):
        """Ids go up in tens because a library's drives take the ids between."""
        self.assertEqual(self.service.next_id().data['library_id'], 40)

    def test_unreadable_config_is_reported(self):
        service = LibraryService(Path(tempfile.mkdtemp()))
        result = service.list()
        self.assertFalse(result.success)
        self.assertIn('Could not read', result.message)


class LibraryInfoTests(TestCase):
    def test_address_needs_all_three_parts(self):
        self.assertIsNone(LibraryInfo(library_id=10, channel=0, target=1).address)
        self.assertEqual(
            LibraryInfo(library_id=10, channel=0, target=1, lun=0).address, (0, 1, 0))

    def test_unit_name_matches_the_systemd_instance(self):
        self.assertEqual(LibraryInfo(library_id=10).unit_name,
                         'vtllibrary@10.service')


class CreateWorkflowTests(TestCase):
    """The sequence that used to live in views.py:432-520."""

    def setUp(self):
        self.spec = {'profile': 'STK', 'library_id': 40, 'num_drives': 2,
                     'media_type': 'LTO8'}

    def _patch(self, *, validate=True, create=True, restart=True,
               recognised=True, media=True):
        created = (success_result('Library 40 created', {'library_id': 40})
                   if create else failure_result('device.conf is not writable',
                                                 ['permission denied']))
        patches = [
            mock.patch.object(
                LibraryService, 'validate',
                return_value=success_result('Specification is valid', {'warnings': []})
                if validate else failure_result('Specification is not valid',
                                                ['unknown profile'])),
            mock.patch.object(LibraryService, 'create', return_value=created),
            mock.patch.object(
                LibraryService, 'restart_services',
                return_value=success_result('Services restarted') if restart
                else failure_result('vtllibrary@40 did not start', ['timeout'])),
            mock.patch.object(LibraryService, 'recognised_by_mhvtl',
                              return_value=recognised),
            mock.patch.object(
                workflow_module, '_create_media',
                return_value=('media', media,
                              'Tape media created' if media else 'mktape failed')),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])

    def steps(self, result):
        return {step['step']: step for step in result.data['steps']}

    def test_happy_path_runs_every_step_in_order(self):
        self._patch()
        result = create_library_workflow(self.spec)
        self.assertTrue(result.success, result.message)
        self.assertEqual([s['step'] for s in result.data['steps']],
                         ['validate', 'create', 'restart', 'verify', 'media'])

    def test_a_rejected_specification_creates_nothing(self):
        self._patch(validate=False)
        with mock.patch.object(LibraryService, 'create') as create:
            result = create_library_workflow(self.spec)
        self.assertFalse(result.success)
        create.assert_not_called()

    def test_a_rejected_specification_still_reports_its_step(self):
        """A caller that only gets a message cannot say which stage refused."""
        self._patch(validate=False)
        result = create_library_workflow(self.spec)
        self.assertEqual(self.steps(result)['validate']['fatal'], True)

    def test_a_failed_creation_is_fatal(self):
        self._patch(create=False)
        result = create_library_workflow(self.spec)
        self.assertFalse(result.success)
        self.assertTrue(self.steps(result)['create']['fatal'])

    def test_a_failed_restart_is_a_warning_not_a_failure(self):
        """The configuration is written; what it needs is a restart, not a rollback."""
        self._patch(restart=False)
        result = create_library_workflow(self.spec)
        self.assertTrue(result.success)
        self.assertFalse(self.steps(result)['restart']['ok'])
        self.assertIn('did not restart', self.steps(result)['restart']['message'])

    def test_a_library_mhvtl_cannot_see_yet_is_a_warning(self):
        self._patch(recognised=False)
        result = create_library_workflow(self.spec)
        self.assertTrue(result.success)
        self.assertIn('does not report it yet', self.steps(result)['verify']['message'])

    def test_missing_media_is_a_warning(self):
        """The library exists; its tapes can be created afterwards."""
        self._patch(media=False)
        result = create_library_workflow(self.spec)
        self.assertTrue(result.success)
        self.assertFalse(self.steps(result)['media']['ok'])
        self.assertFalse(self.steps(result)['media']['fatal'])

    def test_warnings_are_collected_for_the_caller(self):
        self._patch(restart=False, media=False)
        result = create_library_workflow(self.spec)
        # restart and media failed; verify succeeded, so two warnings.
        self.assertEqual(len(result.data['warnings']), 2)
        self.assertTrue(result.success)

    def test_restart_can_be_skipped(self):
        """A CLI creating several libraries restarts once at the end."""
        self._patch()
        result = create_library_workflow(self.spec, restart=False)
        self.assertNotIn('restart', [s['step'] for s in result.data['steps']])
        self.assertNotIn('verify', [s['step'] for s in result.data['steps']])

    def test_media_can_be_skipped(self):
        self._patch()
        result = create_library_workflow(self.spec, create_media=False)
        self.assertNotIn('media', [s['step'] for s in result.data['steps']])


class NoDelegationTests(TestCase):
    """Step 8 finished the library domain: nothing here delegates any more.

    The class this used to wrap, MHVTLLibraryService, defaults config_dir to a
    hardcoded "/etc/mhvtl" and reads no settings - which created a library on
    the live host during this refactor while the caller had passed a scratch
    directory, and the run reported success. The direction is now reversed: that
    class is an adapter over this one.
    """

    def test_the_service_has_no_route_back_to_the_old_one(self):
        self.assertFalse(hasattr(LibraryService, '_legacy'))

    def test_the_old_service_is_gone(self):
        """The adapters package went with the last view that used it."""
        with self.assertRaises(ImportError):
            __import__('apps.libraries.adapters.mhvtl_library_service')


class ValidationTests(TestCase):
    """The vendor-profile rules, applied to a specification.

    These catch the combinations MHVTL would accept into device.conf and then
    fail on - an LTO8 cartridge in a drive that cannot read it.
    """

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.spec = {'library_id': 40, 'profile': 'IBM', 'product': '03584L32',
                     'drive_product': 'ULT3580-TD6', 'media_type': 'LTO6',
                     'num_drives': 2}

    def check(self, **overrides):
        from apps.libraries.services.libraries import validation
        return validation.validate({**self.spec, **overrides}, self.config)

    def test_accepts_a_consistent_specification(self):
        self.assertTrue(self.check().is_valid, self.check().errors)

    def test_refuses_media_the_drive_cannot_read(self):
        result = self.check(media_type='LTO8')
        self.assertFalse(result.is_valid)
        self.assertIn('not supported by drive model', ' '.join(result.errors))

    def test_refuses_an_unknown_profile(self):
        result = self.check(profile='NOSUCH')
        self.assertFalse(result.is_valid)
        self.assertTrue(any('Invalid profile' in e for e in result.errors))

    def test_refuses_a_library_id_already_in_device_conf(self):
        result = self.check(library_id=10)
        self.assertFalse(result.is_valid)
        self.assertIn('already exists', ' '.join(result.errors))

    def test_refuses_an_impossible_drive_count(self):
        self.assertFalse(self.check(num_drives=99).is_valid)
        self.assertFalse(self.check(num_drives=0).is_valid)

    def test_requires_a_library_id(self):
        from apps.libraries.services.libraries import validation
        self.assertFalse(validation.validate({'profile': 'IBM'}, self.config).is_valid)

    def test_a_missing_device_conf_is_not_an_error(self):
        """The first library on a host is created before the file exists."""
        from apps.libraries.services.libraries import validation
        result = validation.validate(self.spec, Path(tempfile.mkdtemp()))
        self.assertTrue(result.is_valid, result.errors)


class RenderingTests(TestCase):
    """Writing a library and its drives into device.conf format."""

    def setUp(self):
        self.spec = {'library_id': 40, 'channel': 0, 'lun': 0, 'target': 18,
                     'vendor': 'IBM', 'product': '03584L32', 'serial': 'XYZZY_40',
                     'profile': 'IBM', 'drive_vendor': 'IBM',
                     'drive_product': 'ULT3580-TD6', 'num_drives': 2}

    def render(self, **overrides):
        from apps.libraries.services.config import device_conf
        return device_conf.render_library_and_drives(
            '', {**self.spec, **overrides}, [19, 20], home_dir='/opt/mhvtl')

    def test_what_is_written_can_be_read_back(self):
        from apps.libraries.services.config import device_conf
        parsed = device_conf.parse(self.render())
        self.assertEqual(list(parsed.libraries), [40])
        self.assertEqual(sorted(parsed.drives), [41, 42])
        self.assertEqual(parsed.drives[41]['library_id'], 40)

    def test_includes_the_scsi_inquiry_fields(self):
        """Backup software identifies a device by these; a library written
        without them is reported differently to Veeam or NetBackup."""
        text = self.render()
        for field in ('Vendor identification', 'Product identification',
                      'Product revision level', 'Unit serial number', 'NAA'):
            self.assertIn(field, text)

    def test_serial_is_truncated_to_the_scsi_field(self):
        text = self.render(serial='A' * 40)
        self.assertIn(' Unit serial number: ' + 'A' * 10 + '\n', text)

    def test_home_directory_is_not_hardcoded(self):
        from apps.libraries.services.config import device_conf
        text = device_conf.render_library_and_drives(
            '', self.spec, [19, 20], home_dir='/srv/tapes')
        self.assertIn(' Home directory: /srv/tapes', text)


class RegenerateContentsTests(TestCase):
    """Rebuilding library_contents for every library in device.conf.

    The repair for a host whose contents files were lost: the libraries are
    declared, the daemons start, and the robots report no slots.
    """

    def setUp(self):
        from apps.libraries.services.config.service import ConfigService
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.service = ConfigService(self.config)

    def test_writes_one_file_per_library(self):
        result = self.service.regenerate_library_contents()
        self.assertTrue(result.success, result.errors)
        for library_id in (10, 20, 30):
            self.assertTrue((self.config / f'library_contents.{library_id}').exists())

    def test_each_file_has_the_library_s_drive_count(self):
        from apps.libraries.services.config import library_contents
        self.service.regenerate_library_contents()
        contents = library_contents.parse(
            (self.config / 'library_contents.10').read_text())
        self.assertEqual(contents.drive_count, 4)

    def test_refuses_to_overwrite_without_force(self):
        (self.config / 'library_contents.10').write_text('VERSION: 2\nSlot 1: KEEP01L8\n')
        result = self.service.regenerate_library_contents()

        self.assertFalse(result.success)
        self.assertIn('KEEP01L8', (self.config / 'library_contents.10').read_text())

    def test_refuses_as_a_whole_not_half_way_through(self):
        """A partial regeneration leaves some libraries old and some new."""
        (self.config / 'library_contents.30').write_text('VERSION: 2\n')
        self.service.regenerate_library_contents()

        self.assertFalse((self.config / 'library_contents.10').exists(),
                         'nothing should have been written')

    def test_force_overwrites(self):
        (self.config / 'library_contents.10').write_text('VERSION: 2\nSlot 1: KEEP01L8\n')
        result = self.service.regenerate_library_contents(force=True)

        self.assertTrue(result.success, result.errors)
        self.assertNotIn('KEEP01L8', (self.config / 'library_contents.10').read_text())

    def test_an_unreadable_device_conf_is_a_failure_not_an_empty_success(self):
        from apps.libraries.services.config.service import ConfigService
        result = ConfigService(Path(tempfile.mkdtemp())).regenerate_library_contents()
        self.assertFalse(result.success)


class CreateMediaStepTests(TestCase):
    """The workflow's media step, run for real with only mktape faked.

    CreateWorkflowTests replaces _create_media outright, which is how a call
    with an argument the old adapter did not take went unnoticed: every
    library created from the web form after the cutover got no tapes.
    """

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.media_dir = Path(tempfile.mkdtemp())

    def test_the_step_creates_every_listed_tape(self):
        from apps.libraries.services.core.shell import CommandResult
        from apps.libraries.services.tapes import media

        ok = CommandResult(['mktape'], 0, '', '')
        with mock.patch.object(media, 'create', return_value=ok) as mktape, \
                mock.patch.object(media, 'home_dir', return_value=self.media_dir):
            name, success, message = workflow_module._create_media(
                10, {'media_type': 'LTO8'}, self.config)
        self.assertEqual(name, 'media')
        self.assertTrue(success, message)
        self.assertEqual(mktape.call_count, 32)
        densities = {call.kwargs['barcode'] if 'barcode' in call.kwargs
                     else call.args[0]: call.kwargs['density']
                     for call in mktape.call_args_list}
        self.assertEqual(densities['E01001L8'], 'LTO8')
        self.assertEqual(densities['F01030L6'], 'LTO6')
