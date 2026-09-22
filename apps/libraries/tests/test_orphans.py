"""Orphans: what one part of the system believes in and another does not.

Step 8 of the service-layer refactor. systemd is faked throughout; the file and
device.conf sides are real, against a scratch directory.

The case that matters most is test_an_unreadable_device_conf_cleans_nothing. With
no device.conf every library looks orphaned, and a cleanup that believed the
report would remove every contents file on the host.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.libraries import orphans

FIXTURES = Path(__file__).parent / 'fixtures'

DEVICE_CONF = """\
Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00
 Vendor identification: STK
 Product identification: L700
 Unit serial number: XYZZY_A

Drive: 11 CHANNEL: 00 TARGET: 01 LUN: 00
 Library ID: 10 Slot: 01
 Vendor identification: IBM

Drive: 91 CHANNEL: 00 TARGET: 09 LUN: 00
 Library ID: 90 Slot: 01
 Vendor identification: IBM
"""


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


class FindTests(TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        (self.config / 'device.conf').write_text(DEVICE_CONF)
        (self.config / 'library_contents.10').write_text('VERSION: 2\n')
        (self.config / 'library_contents.90').write_text('VERSION: 2\n')
        (self.config / 'notes.txt').write_text('not ours\n')

        patch = mock.patch.object(orphans.units, 'all_vtl_units', return_value=[])
        self.all_units = patch.start()
        self.addCleanup(patch.stop)

    def test_finds_a_drive_whose_library_is_gone(self):
        found = orphans.find(self.config)
        self.assertEqual([d['drive_id'] for d in found['orphaned_drives']], [91])

    def test_a_drive_with_a_real_library_is_not_an_orphan(self):
        found = orphans.find(self.config)
        self.assertNotIn(11, [d['drive_id'] for d in found['orphaned_drives']])

    def test_finds_a_contents_file_nobody_declares(self):
        found = orphans.find(self.config)
        self.assertEqual([f['library_id'] for f in found['orphaned_files']], [90])

    def test_other_files_in_the_directory_are_left_alone(self):
        found = orphans.find(self.config)
        self.assertNotIn('notes.txt', ' '.join(f['path']
                                               for f in found['orphaned_files']))

    def test_finds_units_for_ids_device_conf_does_not_have(self):
        self.all_units.return_value = ['vtllibrary@10.service',
                                       'vtllibrary@90.service',
                                       'vtltape@11.service', 'vtltape@91.service']
        found = orphans.find(self.config)
        self.assertEqual(sorted(s['service'] for s in found['orphaned_services']),
                         ['vtllibrary@90.service'])

    def test_a_drive_unit_for_a_declared_drive_is_kept(self):
        """Drive 91 is in device.conf even though its library is not.

        Removing its unit and leaving its record would be a second orphan.
        """
        self.all_units.return_value = ['vtltape@91.service']
        self.assertEqual(orphans.find(self.config)['orphaned_services'], [])

    def test_finds_a_tape_on_disk_no_library_lists(self):
        """What `tape delete` without --remove-media leaves: data with no
        library."""
        from apps.libraries.services.tapes import media as tape_media
        with mock.patch.object(tape_media, 'list_media',
                               return_value=['E01001L8', 'E01099L8']):
            (self.config / 'library_contents.10').write_text(
                'VERSION: 2\n\nSlot 1: E01001L8\n')
            found = orphans.find(self.config)
        self.assertEqual([m['barcode'] for m in found['orphaned_media']],
                         ['E01099L8'])

    def test_a_tape_a_library_lists_is_not_an_orphan(self):
        from apps.libraries.services.tapes import media as tape_media
        (self.config / 'library_contents.10').write_text(
            'VERSION: 2\n\nSlot 1: E01001L8\n')
        with mock.patch.object(tape_media, 'list_media',
                               return_value=['E01001L8']):
            found = orphans.find(self.config)
        self.assertEqual(found['orphaned_media'], [])

    def test_a_tape_in_a_contents_file_device_conf_forgot_is_not_an_orphan(self):
        """Library 90 is an orphaned file, but its tapes still belong to it:
        calling them orphans would invite deleting data that a repair of
        device.conf brings back."""
        from apps.libraries.services.tapes import media as tape_media
        (self.config / 'library_contents.90').write_text(
            'VERSION: 2\n\nSlot 1: E90001L8\n')
        with mock.patch.object(tape_media, 'list_media',
                               return_value=['E90001L8']):
            found = orphans.find(self.config)
        self.assertEqual(found['orphaned_media'], [])

    def test_the_valid_ids_are_reported_too(self):
        found = orphans.find(self.config)
        self.assertEqual(found['valid_library_ids'], [10])
        self.assertEqual(found['valid_drive_ids'], [11, 91])

    def test_an_unreadable_device_conf_names_nothing_as_an_orphan(self):
        """Every library would look orphaned."""
        empty = Path(tempfile.mkdtemp())
        (empty / 'library_contents.10').write_text('VERSION: 2\n')

        found = orphans.find(empty)
        self.assertEqual(found['orphaned_files'], [])
        self.assertEqual(found['orphaned_drives'], [])
        self.assertTrue(found['unreadable'])

    def test_the_result_wrapper_counts_what_was_found(self):
        result = orphans.find_result(self.config)
        self.assertTrue(result.success)
        self.assertIn('1 drives', result.message)

    def test_the_result_wrapper_fails_when_the_config_is_unreadable(self):
        self.assertFalse(orphans.find_result(Path(tempfile.mkdtemp())).success)


class CleanupTests(TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        (self.config / 'device.conf').write_text(DEVICE_CONF)
        (self.config / 'library_contents.10').write_text('VERSION: 2\n')
        (self.config / 'library_contents.90').write_text('VERSION: 2\n')

        # cleanup only stops units for the live configuration directory
        from apps.libraries.services.libraries import lifecycle
        live = mock.patch.object(lifecycle, 'config_dir', return_value=self.config)
        live.start()
        self.addCleanup(live.stop)

        for name in ('stop', 'disable', 'reset_failed', 'daemon_reload'):
            patch = mock.patch.object(orphans.units, name, return_value=ok())
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

        for name, value in (('all_vtl_units', ['vtllibrary@90.service']),
                            ('kill_lingering', [])):
            patch = mock.patch.object(orphans.units, name, return_value=value)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def _conf(self):
        from apps.libraries.services.config import device_conf
        return device_conf.parse((self.config / 'device.conf').read_text())

    def test_removes_the_orphaned_drive_record(self):
        result = orphans.cleanup(config_directory=self.config)
        self.assertTrue(result.success, result.errors)
        self.assertNotIn(91, self._conf().drives)

    def test_keeps_the_drive_that_has_a_library(self):
        orphans.cleanup(config_directory=self.config)
        conf = self._conf()
        self.assertIn(11, conf.drives)
        self.assertIn(10, conf.libraries)

    def test_removes_the_orphaned_contents_file_only(self):
        orphans.cleanup(config_directory=self.config)
        self.assertFalse((self.config / 'library_contents.90').exists())
        self.assertTrue((self.config / 'library_contents.10').exists())

    def test_stops_the_orphaned_unit_before_touching_the_configuration(self):
        order = []
        self.stop.side_effect = lambda unit: order.append('stop') or ok()

        from apps.libraries.services.config.service import ConfigService
        real_write = ConfigService.write_device_conf

        def write(service, text, backup=True):
            order.append('write')
            return real_write(service, text, backup=backup)

        with mock.patch.object(ConfigService, 'write_device_conf', write):
            orphans.cleanup(config_directory=self.config)

        self.assertEqual(order, ['stop', 'write'])

    def test_a_dry_run_changes_nothing(self):
        before = (self.config / 'device.conf').read_text()
        result = orphans.cleanup(dry_run=True, config_directory=self.config)

        self.assertTrue(result.success)
        self.assertTrue(result.data['dry_run'])
        self.assertEqual((self.config / 'device.conf').read_text(), before)
        self.assertTrue((self.config / 'library_contents.90').exists())
        self.stop.assert_not_called()

    def test_an_unreadable_device_conf_cleans_nothing(self):
        """With no device.conf every library looks orphaned."""
        empty = Path(tempfile.mkdtemp())
        (empty / 'library_contents.10').write_text('VERSION: 2\n')

        result = orphans.cleanup(config_directory=empty)

        self.assertFalse(result.success)
        self.assertTrue((empty / 'library_contents.10').exists())
        self.stop.assert_not_called()

    def test_nothing_to_do_is_a_success_not_a_failure(self):
        self.all_vtl_units.return_value = []
        (self.config / 'library_contents.90').unlink()
        (self.config / 'device.conf').write_text(
            DEVICE_CONF.split('Drive: 91')[0])

        result = orphans.cleanup(config_directory=self.config)
        self.assertTrue(result.success)
        self.assertIn('Nothing to clean up', result.message)

    def test_a_failed_write_restores_the_configuration(self):
        from apps.libraries.services.config.service import ConfigService
        before = (self.config / 'device.conf').read_text()

        with mock.patch.object(ConfigService, 'write_device_conf',
                               side_effect=OSError('read-only file system')):
            result = orphans.cleanup(config_directory=self.config)

        self.assertFalse(result.success)
        self.assertEqual((self.config / 'device.conf').read_text(), before)
        self.assertTrue((self.config / 'library_contents.90').exists(),
                        'files must not be removed after the write failed')

    def test_a_backup_is_taken_first(self):
        result = orphans.cleanup(config_directory=self.config)
        self.assertTrue(Path(result.data['backup_path'], 'device.conf').exists())

    def test_a_unit_that_will_not_stop_is_reported_not_fatal(self):
        self.stop.side_effect = OSError('systemctl missing')
        result = orphans.cleanup(config_directory=self.config)

        self.assertTrue(result.success)
        self.assertIn('could not stop', ' '.join(result.errors))
        self.assertNotIn(91, self._conf().drives)


    def test_cleanup_never_removes_media(self):
        """A tape on disk that no library lists is reported, never deleted:
        lifecycle.delete(remove_media=True) is where that decision is made."""
        from apps.libraries.services.tapes import media as tape_media
        with mock.patch.object(tape_media, 'list_media',
                               return_value=['E01099L8']), \
             mock.patch.object(tape_media, 'delete') as remove:
            result = orphans.cleanup(config_directory=self.config)
        remove.assert_not_called()
        # Reported, so it can be adopted back - but not in what was cleaned.
        self.assertIn('E01099L8', str(result.data['orphans']['orphaned_media']))
        self.assertNotIn('E01099L8', str(result.data['cleaned']))

class LingeringProcessTests(TestCase):
    """The version this replaces ran `pkill -9 -f "vtltape.*-q<id>"`.

    That matches on a command line, so it kills anything whose command line
    happens to contain the text - including, during this refactor, the shell
    that ran it.
    """

    from apps.libraries.services.console import units as units_module

    def _proc(self, pid, cmdline):
        return mock.patch.object(Path, 'read_bytes',
                                 return_value=cmdline.encode())

    def test_reads_pids_from_systemctl_not_from_a_pattern(self):
        show = 'MainPID=4242\nControlPID=0\nExecMainPID=4242\n'
        with mock.patch.object(self.units_module.shell, 'run',
                               return_value=ok(show)):
            self.assertEqual(self.units_module.pids_of('vtltape@91.service'),
                             [4242])

    def test_kills_only_a_matching_mhvtl_daemon(self):
        with mock.patch.object(self.units_module, 'pids_of', return_value=[4242]), \
             self._proc(4242, 'vtltape\x00-q 91\x00'), \
             mock.patch.object(self.units_module.shell, 'sudo',
                               return_value=ok()) as sudo:
            killed = self.units_module.kill_lingering('vtltape@91.service')

        self.assertEqual(killed, [4242])
        self.assertEqual(sudo.call_args[0][0], ['kill', '-9', '4242'])

    def test_refuses_to_kill_something_that_is_not_an_mhvtl_daemon(self):
        """A pid that exited and was reused must not be killed in its place."""
        with mock.patch.object(self.units_module, 'pids_of', return_value=[4242]), \
             self._proc(4242, '/usr/bin/bash\x00-c\x00something else\x00'), \
             mock.patch.object(self.units_module.shell, 'sudo') as sudo:
            killed = self.units_module.kill_lingering('vtltape@91.service')

        self.assertEqual(killed, [])
        sudo.assert_not_called()

    def test_refuses_to_kill_a_daemon_for_a_different_queue(self):
        with mock.patch.object(self.units_module, 'pids_of', return_value=[4242]), \
             self._proc(4242, 'vtltape\x00-q 20\x00'), \
             mock.patch.object(self.units_module.shell, 'sudo') as sudo:
            self.assertEqual(
                self.units_module.kill_lingering('vtltape@91.service'), [])
        sudo.assert_not_called()

    def test_a_process_that_has_already_exited_is_skipped(self):
        with mock.patch.object(self.units_module, 'pids_of', return_value=[4242]), \
             mock.patch.object(Path, 'read_bytes', side_effect=OSError('gone')), \
             mock.patch.object(self.units_module.shell, 'sudo') as sudo:
            self.assertEqual(
                self.units_module.kill_lingering('vtltape@91.service'), [])
        sudo.assert_not_called()
