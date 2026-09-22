"""SCSI discovery, device mapping, and the mtx/mt/vtlcmd wrappers.

Step 4 of the service-layer refactor. The parsers run against the captured
fixtures; the operations run against a fake mtx, because the alternative is
moving real tapes to find out whether an error message is right.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.operations import OperationsService, mt, mtx, vtlcmd
from apps.libraries.services.scsi import lsscsi, mapping
from apps.libraries.services.scsi.models import ScsiAddress

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return (FIXTURES / name).read_text()


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


def failed(stderr='', stdout=''):
    return CommandResult(['fake'], 1, stdout, stderr)


class LsscsiParsingTests(TestCase):
    def setUp(self):
        self.devices = lsscsi.parse(fixture('lsscsi-g.txt'))

    def test_parses_every_device(self):
        self.assertEqual(len(self.devices), 18)
        self.assertEqual(len(lsscsi.changers(self.devices)), 3)
        self.assertEqual(len(lsscsi.tapes(self.devices)), 12)

    def test_keeps_the_scsi_address_as_numbers(self):
        changer = lsscsi.changers(self.devices)[0]
        self.assertEqual(changer.address.ctl, (0, 0, 0))
        self.assertEqual(str(changer.address), '[16:0:0:0]')

    def test_separates_the_device_and_generic_nodes(self):
        changer = lsscsi.changers(self.devices)[0]
        self.assertEqual(changer.device_path, '/dev/sch1')
        self.assertEqual(changer.generic_path, '/dev/sg4')
        self.assertEqual(changer.preferred_path, '/dev/sg4')

    def test_reads_vendor_and_model(self):
        changer = lsscsi.changers(self.devices)[0]
        self.assertEqual(changer.vendor, 'STK')
        self.assertEqual(changer.model, 'L700')

    def test_model_containing_a_space_no_longer_shifts_the_columns(self):
        """The old parser split on whitespace and lost the device path here."""
        device = lsscsi.parse_line(
            '[5:0:1:0]    tape    HP       Ultrium 6-SCSI   1.00  /dev/st9   /dev/sg20')
        self.assertEqual(device.vendor, 'HP')
        self.assertEqual(device.model, 'Ultrium 6-SCSI')
        self.assertEqual(device.revision, '1.00')
        self.assertEqual(device.device_path, '/dev/st9')
        self.assertEqual(device.generic_path, '/dev/sg20')

    def test_rubbish_is_ignored_not_raised(self):
        self.assertIsNone(lsscsi.parse_line('lsscsi: command not found'))
        self.assertEqual(lsscsi.parse('\n\ngarbage\n'), [])

    def test_address_parsing(self):
        self.assertIsNone(ScsiAddress.parse('not an address'))
        self.assertEqual(ScsiAddress.parse('[16:0:13:0]').ctl, (0, 13, 0))


class MappingTests(TestCase):
    """The mapping rule: match on the address, never on position."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.devices = lsscsi.parse(fixture('lsscsi-g.txt'))

    def test_library_resolves_to_the_changer_at_its_address(self):
        for library_id, node in ((10, '/dev/sg4'), (20, '/dev/sg5'), (30, '/dev/sg3')):
            with self.subTest(library=library_id):
                self.assertEqual(
                    mapping.device_for_library(library_id, devices=self.devices,
                                               config_dir=self.config),
                    node)

    def test_position_matching_would_have_disagreed(self):
        """device.conf lists 10, 30, 20; lsscsi lists targets 0, 8, 13.

        The second changer lsscsi reports is library 30's. Picking by position
        would hand library 20 that device and act on the wrong library.
        """
        second_by_position = lsscsi.changers(self.devices)[1].preferred_path
        by_address = mapping.device_for_library(20, devices=self.devices,
                                                config_dir=self.config)
        self.assertEqual(second_by_position, '/dev/sg3')
        self.assertEqual(by_address, '/dev/sg5')

    def test_drive_resolves_to_its_tape_node(self):
        self.assertEqual(
            mapping.device_for_drive(21, devices=self.devices, config_dir=self.config),
            '/dev/st5')

    def test_unknown_ids_resolve_to_nothing(self):
        self.assertIsNone(mapping.device_for_library(99, devices=self.devices,
                                                     config_dir=self.config))
        self.assertIsNone(mapping.device_for_drive(99, devices=self.devices,
                                                   config_dir=self.config))

    def test_an_address_no_device_reports_is_not_guessed(self):
        """Better no device than the wrong one."""
        only_first = [lsscsi.changers(self.devices)[0]]
        self.assertIsNone(mapping.device_for_library(20, devices=only_first,
                                                     config_dir=self.config))


class MtxParsingTests(TestCase):
    def setUp(self):
        self.status = mtx.parse(fixture('mtx-status-lib10.txt'))

    def test_reads_the_header(self):
        self.assertEqual(self.status.device, '/dev/sg4')
        self.assertEqual(self.status.declared_drives, 4)
        self.assertEqual(self.status.declared_slots, 43)

    def test_counts_elements(self):
        self.assertEqual(self.status.summary,
                         {'total_slots': 39, 'full_slots': 31, 'empty_slots': 8,
                          'total_drives': 4, 'loaded_drives': 1, 'ie_slots': 4})

    def test_reads_a_loaded_drive_and_its_origin(self):
        drive = self.status.drive(0)
        self.assertTrue(drive.full)
        self.assertEqual(drive.barcode, 'E01003L8')
        self.assertEqual(drive.slot_origin, 3)

    def test_finds_a_tape_wherever_it_is(self):
        self.assertEqual(self.status.find_barcode('E01003L8').number, 0)
        self.assertEqual(self.status.find_barcode('E01001L8').number, 1)
        self.assertIsNone(self.status.find_barcode('NOSUCH'))

    def test_import_export_slots_are_separate(self):
        self.assertEqual(len(self.status.import_export), 4)
        self.assertTrue(all(s.import_export for s in self.status.import_export))


class MtAndVtlcmdParsingTests(TestCase):
    def test_mt_status(self):
        status = mt.parse('/dev/nst0', fixture('mt-status-drive.txt'))
        self.assertTrue(status.online)
        self.assertFalse(status.has_medium)          # file/block -1 means no tape
        self.assertEqual(status.density_name, 'LTO-8')

    def test_mt_reports_a_busy_drive(self):
        status = mt.parse('/dev/nst0', '/dev/nst0: Device or resource busy')
        self.assertTrue(status.busy)
        self.assertFalse(status.online)

    def test_vtlcmd_stats_when_idle(self):
        stats = vtlcmd.parse_stats(fixture('vtlcmd-stats.txt'))
        self.assertIsNone(stats.barcode)
        self.assertFalse(stats.loaded)

    def test_vtlcmd_stats_during_a_write(self):
        stats = vtlcmd.parse_stats(
            'Tape: E01001L8 Loaded: Yes Written: 41943040 Read: 0 '
            'WMedia: 199040 RMedia: 0 Capacity: 524288000')
        self.assertEqual(stats.barcode, 'E01001L8')
        self.assertTrue(stats.loaded)
        self.assertEqual(stats.written, 41943040)
        self.assertEqual(stats.compression_ratio, 210.73)

    def test_unrecognised_output_is_not_a_crash(self):
        self.assertIsNone(vtlcmd.parse_stats('Command for tape not allowed'))


class VtlcmdAddressingTests(TestCase):
    """The queue id is the device.conf id, not library_id // 10."""

    def test_online_addresses_the_library_id(self):
        with mock.patch.object(vtlcmd.shell, 'sudo', return_value=ok()) as sudo:
            vtlcmd.online(10)
        self.assertEqual(sudo.call_args[0][0], ['vtlcmd', '10', 'online'])

    def test_not_a_derived_index(self):
        """The old code sent `vtlcmd 1` for library 10; there is no queue 1."""
        with mock.patch.object(vtlcmd.shell, 'sudo', return_value=ok()) as sudo:
            vtlcmd.offline(20)
        self.assertNotIn('2', sudo.call_args[0][0])
        self.assertEqual(sudo.call_args[0][0], ['vtlcmd', '20', 'offline'])

    def test_drive_stats_address_the_drive_id(self):
        with mock.patch.object(vtlcmd.shell, 'sudo', return_value=ok()) as sudo:
            vtlcmd.stats(21)
        self.assertEqual(sudo.call_args[0][0], ['vtlcmd', '21', 'stats'])


class OperationsTests(TestCase):
    """mtx is faked: the point is the decisions, not the robot."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.ops = OperationsService(self.config)
        self.status = mtx.parse(fixture('mtx-status-lib10.txt'))

    def _patched(self, load_result=None):
        return (
            mock.patch.object(mapping, 'device_for_library', return_value='/dev/sg4'),
            mock.patch.object(mtx, 'status', return_value=self.status),
            mock.patch.object(mtx, 'load', return_value=load_result or ok()),
            mock.patch.object(mtx, 'unload', return_value=load_result or ok()),
            mock.patch.object(mtx, 'transfer', return_value=load_result or ok()),
        )

    def run_with_fakes(self, call, load_result=None):
        patches = self._patched(load_result)
        for patch in patches:
            patch.start()
        try:
            return call()
        finally:
            for patch in patches:
                patch.stop()

    def test_mount_reports_the_barcode_it_moved(self):
        result = self.run_with_fakes(lambda: self.ops.mount(10, 1, 1))
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['barcode'], 'E01001L8')

    def test_mount_refuses_an_empty_slot(self):
        empty = next(s.number for s in self.status.slots if not s.full)
        result = self.run_with_fakes(lambda: self.ops.mount(10, empty, 1))
        self.assertFalse(result.success)
        self.assertIn('empty', result.message)

    def test_mount_refuses_an_occupied_drive(self):
        result = self.run_with_fakes(lambda: self.ops.mount(10, 1, 0))
        self.assertFalse(result.success)
        self.assertIn('already holds', result.message)

    def test_mount_refuses_a_slot_that_does_not_exist(self):
        result = self.run_with_fakes(lambda: self.ops.mount(10, 999, 1))
        self.assertFalse(result.success)
        self.assertIn('does not exist', result.message)

    def test_mount_refuses_a_cartridge_the_drive_does_not_load(self):
        """Slot 30 holds F01030L6; mtx drive 1 is device.conf drive 12, an
        ULT3580-TD8, which MHVTL gives LTO-8 and LTO-7 only. The robot must
        not move it: the drive would unload it and keep it."""
        load = mock.MagicMock(return_value=ok())
        with mock.patch.object(mapping, 'device_for_library', return_value='/dev/sg4'), \
                mock.patch.object(mtx, 'status', return_value=self.status), \
                mock.patch.object(mtx, 'load', load):
            result = self.ops.mount(10, 30, 1)
        self.assertFalse(result.success)
        self.assertIn('too old', result.message)
        load.assert_not_called()

    def test_force_mounts_it_anyway(self):
        result = self.run_with_fakes(lambda: self.ops.mount(10, 30, 1, force=True))
        self.assertTrue(result.success, result.message)
        self.assertFalse(result.data['compatibility']['compatible'])

    def test_mount_into_a_drive_that_takes_the_cartridge(self):
        """mtx drive 3 is drive 14, an ULT3580-TD6: LTO-6 is its own."""
        result = self.run_with_fakes(lambda: self.ops.mount(10, 30, 3))
        self.assertTrue(result.success, result.message)
        self.assertTrue(result.data['compatibility']['can_write'])
        self.assertNotIn('read-only', result.message)

    def test_an_unreadable_device_conf_does_not_block_a_mount(self):
        """Without a drive model there is nothing to check against; MHVTL
        decides, as it did before the check existed."""
        (self.config / 'device.conf').unlink()
        result = self.run_with_fakes(lambda: self.ops.mount(10, 30, 1))
        self.assertTrue(result.success, result.message)
        self.assertFalse(result.data['compatibility']['known'])

    def test_unmount_returns_the_tape_to_its_own_slot(self):
        """The drive line records where the tape came from; use it."""
        result = self.run_with_fakes(lambda: self.ops.unmount(10, 0))
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['slot'], 3)
        self.assertEqual(result.data['barcode'], 'E01003L8')

    def test_unmount_refuses_an_empty_drive(self):
        result = self.run_with_fakes(lambda: self.ops.unmount(10, 1))
        self.assertFalse(result.success)
        self.assertIn('empty', result.message)

    def test_move_refuses_an_occupied_destination(self):
        result = self.run_with_fakes(lambda: self.ops.move(10, 1, 2))
        self.assertFalse(result.success)
        self.assertIn('already holds', result.message)

    def test_mtx_failure_is_reported_with_its_output(self):
        result = self.run_with_fakes(
            lambda: self.ops.mount(10, 1, 1),
            load_result=failed('mtx: Request Sense: Additional Sense Code = 3B'))
        self.assertFalse(result.success)
        self.assertIn('3B', ' '.join(result.errors))

    def test_a_library_with_no_device_says_so_usefully(self):
        with mock.patch.object(mapping, 'device_for_library', return_value=None):
            result = self.ops.mount(10, 1, 1)
        self.assertFalse(result.success)
        self.assertIn('No device found', result.message)
        self.assertIn('vtllibrary@10', ' '.join(result.errors))


class DriveStatusFlagTests(TestCase):
    """The flags on mt's "General status bits" line.

    Moved from tape_operations_service._parse_mt_status in step 8, where they
    were matched as substrings of the whole output - and the same output carries
    a hex status value, so a search for BOT found it inside "General status bits
    on (41010000)" often enough to matter.
    """
    LOADED = """SCSI 2 tape drive:
File number=0, block number=0, partition=0.
Tape block size 0 bytes. Density code 0x58 (LTO-6).
Soft error count since last status=0
General status bits on (41010000):
 BOT ONLINE IM_REP_EN
"""
    WRITE_PROTECTED = """SCSI 2 tape drive:
File number=0, block number=0, partition=0.
Tape block size 0 bytes. Density code 0x58 (LTO-6).
General status bits on (85010000):
 WR_PROT ONLINE IM_REP_EN
"""
    EMPTY = """SCSI 2 tape drive:
File number=-1, block number=-1, partition=-1.
Tape block size 0 bytes. Density code 0x0 (default).
General status bits on (50000):
 DR_OPEN IM_REP_EN
"""

    def test_reads_the_flags_of_a_loaded_drive(self):
        state = mt.parse('/dev/st0', self.LOADED)
        self.assertTrue(state.at_bot)
        self.assertTrue(state.online)
        self.assertFalse(state.write_protected)
        self.assertFalse(state.door_open)
        self.assertTrue(state.ready)

    def test_reads_write_protection(self):
        self.assertTrue(mt.parse('/dev/st0', self.WRITE_PROTECTED).write_protected)

    def test_an_open_door_is_not_ready(self):
        state = mt.parse('/dev/st0', self.EMPTY)
        self.assertTrue(state.door_open)
        self.assertFalse(state.ready)

    def test_an_empty_drive_has_no_medium(self):
        """File number -1 is the honest test; the old one was "online and the
        door is shut", which called an empty online drive loaded."""
        self.assertFalse(mt.parse('/dev/st0', self.EMPTY).has_medium)
        self.assertTrue(mt.parse('/dev/st0', self.LOADED).has_medium)

    def test_the_hex_status_value_is_not_read_as_a_flag(self):
        """`General status bits on (41010000)` contains no flag names, but a
        substring search for EOT or BOT can still hit the surrounding text."""
        text = 'General status bits on (41010000):\n ONLINE IM_REP_EN\n'
        state = mt.parse('/dev/st0', text)
        self.assertFalse(state.at_bot)
        self.assertFalse(state.at_eot)

    def test_the_flags_reach_the_dict(self):
        data = mt.parse('/dev/st0', self.LOADED).to_dict()
        self.assertTrue(data['at_bot'])
        self.assertTrue(data['ready'])
        self.assertFalse(data['write_protected'])


class MapCommandTests(TestCase):
    """The MAP is the mail slot, and none of its five verbs worked.

    tape_operations_service computed the vtlcmd queue id as `library_id // 10`,
    so every MAP command on library 10 went to queue 1 - which does not exist.
    The queue id is the device.conf id.
    """

    def setUp(self):
        self.service = OperationsService()

    def test_the_queue_id_is_the_library_id(self):
        with mock.patch.object(vtlcmd.shell, 'sudo',
                               return_value=CommandResult(['vtlcmd'], 0, '', '')) as sudo:
            result = self.service.map_command(10, 'open')

        self.assertTrue(result.success, result.errors)
        self.assertEqual(sudo.call_args[0][0], ['vtlcmd', '10', 'open', 'map'])

    def test_every_verb_reaches_vtlcmd(self):
        for action, expected in (('open', 'open'), ('close', 'close'),
                                 ('load', 'load'), ('list', 'list'),
                                 ('empty', 'empty')):
            with self.subTest(action=action):
                with mock.patch.object(
                        vtlcmd.shell, 'sudo',
                        return_value=CommandResult(['vtlcmd'], 0, '', '')) as sudo:
                    self.service.map_command(20, action)
                self.assertEqual(sudo.call_args[0][0],
                                 ['vtlcmd', '20', expected, 'map'])

    def test_an_unknown_verb_never_reaches_vtlcmd(self):
        """Reachable from a web form; vtlcmd takes verbs a form should not."""
        with mock.patch.object(vtlcmd.shell, 'sudo') as sudo:
            result = self.service.map_command(10, 'delete')
        self.assertFalse(result.success)
        self.assertIn('expected', ' '.join(result.errors))
        sudo.assert_not_called()

    def test_a_refusal_from_the_daemon_is_reported(self):
        with mock.patch.object(vtlcmd.shell, 'sudo',
                               return_value=CommandResult(['vtlcmd'], 1, '',
                                                          'No such queue')):
            result = self.service.map_command(10, 'open')

        self.assertFalse(result.success)
        self.assertIn('No such queue', ' '.join(result.errors))

    def test_the_verb_is_case_insensitive(self):
        with mock.patch.object(vtlcmd.shell, 'sudo',
                               return_value=CommandResult(['vtlcmd'], 0, '', '')):
            self.assertTrue(self.service.map_command(10, 'OPEN').success)


class MountingTests(TestCase):
    """services/operations/mounting: what the mount page reads.

    Library 10's fixture: mtx drives 0 and 1 are ULT3580-TD8, 2 and 3 are
    ULT3580-TD6; slot 30 holds F01030L6 and drive 0 holds E01003L8.
    """

    def setUp(self):
        from apps.libraries.services.operations import mounting
        self.mounting = mounting
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.status = mtx.parse(fixture('mtx-status-lib10.txt'))
        for patch in (mock.patch.object(mapping, 'device_for_library',
                                        return_value='/dev/sg4'),
                      mock.patch.object(mtx, 'status', return_value=self.status)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_drives_are_numbered_as_mtx_numbers_them(self):
        drives = self.mounting.library_drives(10, self.config)
        self.assertEqual([(d['drive_num'], d['drive_id'], d['model']) for d in drives],
                         [(0, 11, 'ULT3580-TD8'), (1, 12, 'ULT3580-TD8'),
                          (2, 13, 'ULT3580-TD6'), (3, 14, 'ULT3580-TD6')])
        self.assertEqual(drives[2]['lto_generation'], 'LTO-6')

    def test_the_status_carries_models_and_verdicts(self):
        result = self.mounting.mount_status(10, self.config)
        self.assertTrue(result.success, result.message)
        data = result.data
        self.assertEqual(data['drives'][0]['model'], 'ULT3580-TD8')
        self.assertEqual(data['drives'][0]['tape_lto'], 'LTO-8')
        slot30 = next(s for s in data['storage_slots'] if s['slot_num'] == 30)
        self.assertEqual(slot30['tape_density'], 'LTO6')
        self.assertFalse(data['mount_matrix'][30][1]['compatible'])
        self.assertTrue(data['mount_matrix'][30][3]['can_write'])
        usable = next(c for c in data['compatibility_info'] if c['slot_num'] == 30)
        self.assertEqual([d['drive_num'] for d in usable['compatible_drives']], [2, 3])
        # the page reads these keys
        for key in ('storage_slots', 'import_export_slots', 'slot_summary',
                    'drive_info', 'device_path'):
            self.assertIn(key, data)

    def test_a_library_with_no_device_is_a_failure_not_an_exception(self):
        with mock.patch.object(mapping, 'device_for_library', return_value=None):
            result = self.mounting.mount_status(10, self.config)
        self.assertFalse(result.success)
        self.assertIn('vtllibrary@10', ' '.join(result.errors))

    def test_the_single_check_says_why(self):
        check = self.mounting.mount_check
        self.assertIn('too old', check(10, 30, 1, self.config)['message'])
        self.assertTrue(check(10, 30, 3, self.config)['compatible'])
        self.assertIn('already holds', check(10, 30, 0, self.config)['message'])
        empty = next(s.number for s in self.status.slots if not s.full)
        self.assertIn('no tape', check(10, empty, 3, self.config)['message'])
        self.assertIn('does not exist', check(10, 30, 9, self.config)['message'])
