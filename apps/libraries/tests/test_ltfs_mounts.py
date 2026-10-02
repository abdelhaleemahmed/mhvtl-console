"""services/ltfs - the mount reader and the service's refusals.

The reader is pure text parsing, so it is tested on text. The service is tested
for the order it refuses things in: every verb shares _resolve(), and a caller
should be told the first real problem rather than the last symptom.
"""
from unittest import mock

from django.test import TestCase, override_settings

from apps.libraries.services.ltfs import mounts, service


class MountReaderTests(TestCase):

    SAMPLE = (
        'proc /proc proc rw,nosuid 0 0\n'
        'ltfs:/dev/sg24 /var/lib/ltfs/mnt/library60-drive0 fuse rw,nosuid 0 0\n'
        '/dev/sda2 / xfs rw 0 0\n'
        'ltfs:/dev/sg25 /var/lib/ltfs/mnt/library70-drive0 fuse.ltfs rw 0 0\n'
        'gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw 0 0\n'
    )

    def test_only_ltfs_fuse_mounts_are_reported(self):
        found = mounts.by_device(self.SAMPLE)
        self.assertEqual(found, {
            '/dev/sg24': '/var/lib/ltfs/mnt/library60-drive0',
            '/dev/sg25': '/var/lib/ltfs/mnt/library70-drive0',
        })

    def test_another_fuse_filesystem_is_not_ours(self):
        self.assertNotIn('gvfsd-fuse', str(mounts.by_device(self.SAMPLE)))

    def test_the_device_node_comes_from_the_mount_source(self):
        """Which is why no registry is needed: a mount names its own drive."""
        self.assertEqual(mounts.point_for('/dev/sg24', self.SAMPLE),
                         '/var/lib/ltfs/mnt/library60-drive0')

    def test_an_unmounted_node_is_none(self):
        self.assertIsNone(mounts.point_for('/dev/sg99', self.SAMPLE))
        self.assertIsNone(mounts.point_for(None, self.SAMPLE))

    def test_escaped_paths_are_decoded(self):
        text = 'ltfs:/dev/sg1 /mnt/with\\040space fuse rw 0 0\n'
        self.assertEqual(mounts.by_device(text), {'/dev/sg1': '/mnt/with space'})

    def test_short_and_empty_lines_do_not_raise(self):
        self.assertEqual(mounts.by_device('\nltfs:\nbroken line\n'), {})

    def test_an_unreadable_proc_reports_nothing_mounted(self):
        """The safe direction: the worst outcome is offering to mount something
        already mounted, and ltfs refuses that itself."""
        from pathlib import Path
        with mock.patch.object(mounts, 'PROC_MOUNTS',
                               Path('/proc/no-such-mounts-file')):
            self.assertEqual(mounts.by_device(), {})


class MountPointTests(TestCase):

    def test_one_directory_per_drive_under_the_root(self):
        point = service.mount_point_for(60, 0)
        self.assertEqual(str(point), '/var/lib/ltfs/mnt/library60-drive0')
        self.assertEqual(service.MOUNT_ROOT, point.parent)

    def test_two_drives_do_not_share_a_point(self):
        self.assertNotEqual(service.mount_point_for(60, 0),
                            service.mount_point_for(60, 1))


class RefusalOrderTests(TestCase):
    """What a caller is told, and in which order."""

    def setUp(self):
        self.service = service.LtfsService()

    def test_a_library_with_no_drives_says_so(self):
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=[]):
            result = self.service.status(999)
        self.assertFalse(result.success)
        self.assertIn('no drives', result.message)

    def test_an_unknown_drive_number_names_the_range(self):
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': 'IBM',
                   'model': 'ULT3580-TD7'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives):
            result = self.service.mount(60, 7)
        self.assertFalse(result.success)
        self.assertIn('no drive 7', result.message)

    def test_a_drive_with_no_device_node_is_reported_before_the_cartridge(self):
        """The daemon may not be running; that is the useful thing to say."""
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': 'IBM',
                   'model': 'ULT3580-TD7'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.lsscsi, 'local', return_value=[]), \
             mock.patch.object(service.mapping, 'device_for_drive',
                               return_value=None):
            result = self.service.mount(60, 0)
        self.assertFalse(result.success)
        self.assertIn('no device node', result.message)

    def test_an_unknown_drive_number_costs_no_subprocess(self):
        """A drive that is not in device.conf is a pure configuration question,
        so it is answered before anything is discovered or asked."""
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': 'IBM',
                   'model': 'ULT3580-TD7'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.lsscsi, 'local') as discovered, \
             mock.patch.object(service.shell, 'sudo') as ran:
            result = self.service.mount(60, 9)
        self.assertFalse(result.success)
        discovered.assert_not_called()
        ran.assert_not_called()

    def test_an_empty_drive_is_reported_without_running_ltfs(self):
        """Saying "no cartridge" needs one lsscsi and one mtx - the node comes
        from the first and the cartridge from the second, and neither can be
        known without asking. What must not happen is a mount attempt."""
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': 'IBM',
                   'model': 'ULT3580-TD7'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.lsscsi, 'local', return_value=[]), \
             mock.patch.object(service.mapping, 'device_for_drive',
                               return_value='/dev/sg9'), \
             mock.patch.object(service.mapping, 'device_for_library',
                               return_value=None), \
             mock.patch.object(service.shell, 'sudo') as ran:
            result = self.service.mount(60, 0)
        self.assertFalse(result.success)
        self.assertIn('no cartridge', result.message)
        self.assertEqual([c.args[0][0] for c in ran.call_args_list], [])

    def test_unmounting_nothing_is_success_not_failure(self):
        """Idempotent: a caller asking twice has not made a mistake."""
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': 'IBM',
                   'model': 'ULT3580-TD7'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.mapping, 'device_for_drive',
                               return_value='/dev/sg9'), \
             mock.patch.object(service.mapping, 'device_for_library',
                               return_value=None), \
             mock.patch.object(service.lsscsi, 'local', return_value=[]), \
             mock.patch.object(service.mounts, 'point_for', return_value=None):
            result = self.service.unmount(60, 0)
        self.assertTrue(result.success)
        self.assertIn('no mounted volume', result.message)

    def test_a_mount_point_outside_our_tree_is_refused(self):
        """The sudoers rule allows only children of MOUNT_ROOT; say so first."""
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': 'IBM',
                   'model': 'ULT3580-TD7'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.mapping, 'device_for_drive',
                               return_value='/dev/sg9'), \
             mock.patch.object(service.mapping, 'device_for_library',
                               return_value=None), \
             mock.patch.object(service.lsscsi, 'local', return_value=[]), \
             mock.patch.object(service.mounts, 'point_for',
                               return_value='/mnt/somewhere-else'), \
             mock.patch.object(service.shell, 'sudo') as ran:
            result = self.service.unmount(60, 0)
        self.assertFalse(result.success)
        self.assertIn('not inside', result.message)
        self.assertNotIn('fusermount',
                         [c.args[0][0] for c in ran.call_args_list])

    @override_settings(MHVTL_GUI_ALLOW_LTFS_FORMAT=False)
    def test_formatting_is_refused_in_our_own_words(self):
        """Not left to fail as a sudo error - the refusal says what it is."""
        with mock.patch.object(service.shell, 'sudo') as ran:
            result = self.service.format_cartridge(60, 0)
        self.assertFalse(result.success)
        self.assertIn('disabled', result.message)
        self.assertTrue(any('no undo' in e for e in result.errors))
        ran.assert_not_called()

    @override_settings(MHVTL_GUI_ALLOW_LTFS_FORMAT=True)
    def test_formatting_still_refuses_a_drive_ltfs_cannot_open(self):
        drives = [{'drive_num': 0, 'drive_id': 51, 'vendor': 'STK',
                   'model': 'ULT3580-TD8'}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.mapping, 'device_for_drive',
                               return_value='/dev/sg9'), \
             mock.patch.object(service.mapping, 'device_for_library',
                               return_value=None), \
             mock.patch.object(service.lsscsi, 'local', return_value=[]), \
             mock.patch.object(service.mounts, 'point_for', return_value=None), \
             mock.patch.object(service.shell, 'sudo') as ran:
            result = self.service.format_cartridge(60, 0)
        self.assertFalse(result.success)
        self.assertNotIn('mkltfs', [c.args[0][0] for c in ran.call_args_list])


class FirmwareGateTests(TestCase):
    """The gate is applied to the drive in front of us, not just named.

    device_conf.FIELDS did not read `Product revision level`, so the revision
    never reached ltfs_support.supports() and status() could report which
    firmware a family needs without saying whether this drive met it.
    """

    def setUp(self):
        self.service = service.LtfsService()

    def _row(self, vendor, model, revision):
        drives = [{'drive_num': 0, 'drive_id': 61, 'vendor': vendor,
                   'model': model, 'revision': revision}]
        with mock.patch.object(service.mounting, 'library_drives',
                              return_value=drives), \
             mock.patch.object(service.lsscsi, 'local', return_value=[]), \
             mock.patch.object(service.mapping, 'device_for_library',
                               return_value=None), \
             mock.patch.object(service.mapping, 'device_for_drive',
                               return_value=None):
            result = self.service.status(60)
        self.assertTrue(result.success, result.message)
        return result.data['drives'][0]

    def test_an_ibm_lto8_below_hb81_is_refused_by_the_row(self):
        row = self._row('IBM', 'ULT3580-TD8', 'D.02')
        self.assertFalse(row['ltfs_capable'])
        self.assertIn('HB81', row['ltfs_reason'])

    def test_the_same_drive_at_hb82_is_accepted(self):
        self.assertTrue(self._row('IBM', 'ULT3580-TD8', 'HB82')['ltfs_capable'])

    def test_an_hpe_lto8_at_the_same_low_revision_is_accepted(self):
        """No firmware check exists for VENDOR_HP (vendor_compat.c:347)."""
        self.assertTrue(
            self._row('HPE', 'Ultrium 8-SCSI', 'D.02')['ltfs_capable'])

    def test_the_revision_is_reported_on_the_row(self):
        self.assertEqual(self._row('IBM', 'ULT3580-TD7', 'D.02')['revision'],
                         'D.02')


class InstalledToolsTests(TestCase):

    def test_missing_binaries_report_none_rather_than_raising(self):
        with mock.patch.object(service.Path, 'exists', return_value=False), \
             mock.patch.object(service.shutil, 'which', return_value=None):
            self.assertEqual(set(service.installed().values()), {None})
