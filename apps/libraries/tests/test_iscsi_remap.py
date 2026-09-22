"""Repointing saved pscsi backstores by SCSI address.

A pscsi backstore is saved by /dev/sgN node and restored by it at boot, and the
node numbers change across reboots. After one reboot here the library10 target's
LUN 0, saved as "lib10_changer", was library 20's robot. These tests use the
real saveconfig.json and device.conf captured from that host, with the device
numbering from after the reboot.

The rule under test: a backstore is repointed only when its name says which
device it is and that device is visible. Anything else is left alone - exporting
nothing is recoverable, exporting the wrong robot is how tapes get overwritten.
"""
import json
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.config import device_conf
from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.iscsi import remap
from apps.libraries.services.scsi.models import ScsiAddress, ScsiDevice

FIXTURES = Path(__file__).parent / 'fixtures'


def device(target, kind, model, generic):
    return ScsiDevice(address=ScsiAddress(16, 0, target, 0), device_type=kind,
                      vendor='X', model=model, revision='1',
                      device_path=None, generic_path=generic)


# The numbering after the reboot: library 10's changer moved from sg4 to sg5,
# and sg4 became library 20's.
AFTER_REBOOT = [
    device(0, 'mediumx', 'L700', '/dev/sg5'),       # library 10
    device(1, 'tape', 'ULT3580-TD8', '/dev/sg8'),   # drive 11
    device(2, 'tape', 'ULT3580-TD8', '/dev/sg11'),  # drive 12
    device(3, 'tape', 'ULT3580-TD6', '/dev/sg15'),  # drive 13
    device(4, 'tape', 'ULT3580-TD6', '/dev/sg6'),   # drive 14
    device(13, 'mediumx', 'LIB-302', '/dev/sg4'),   # library 20
]


class PlanTests(TestCase):
    def setUp(self):
        self.saved = json.loads((FIXTURES / 'targetcli-saveconfig.json').read_text())
        self.conf = device_conf.parse((FIXTURES / 'device.conf').read_text())

    def _plan(self, devices=AFTER_REBOOT):
        return {change.name: change for change in
                remap.plan(self.saved, self.conf, devices)}

    def test_the_changer_goes_back_to_library_10s_robot(self):
        """The case that prompted this: sg4 is library 20's changer now."""
        change = self._plan()['lib10_changer']
        self.assertEqual((change.saved, change.wanted), ('/dev/sg4', '/dev/sg5'))
        self.assertIn('LIB-302', change.reason)

    def test_drives_follow_their_position_in_the_library(self):
        plan = self._plan()
        self.assertEqual(plan['lib10_drive0'].wanted, '/dev/sg8')    # drive 11
        self.assertEqual(plan['lib10_drive1'].wanted, '/dev/sg11')   # drive 12

    def test_a_correct_backstore_is_not_a_change(self):
        devices = [device(0, 'mediumx', 'L700', '/dev/sg4')] + AFTER_REBOOT[1:5]
        change = self._plan(devices)['lib10_changer']
        self.assertFalse(change.changes)
        self.assertEqual(change.reason, 'already correct')

    def test_a_device_that_is_not_visible_is_withheld(self):
        """Its daemon has not started. Leaving the old node in place would have
        target.service open whatever device has that number after the reboot."""
        without_changer = AFTER_REBOOT[1:]
        change = self._plan(without_changer)['lib10_changer']
        self.assertTrue(change.withhold)
        self.assertIsNone(change.wanted)
        self.assertIn('withheld', change.reason)

    def test_a_name_this_tool_did_not_create_is_left_alone(self):
        self.saved['storage_objects'][0]['name'] = 'backup_disk'
        change = self._plan()['backup_disk']
        self.assertFalse(change.changes)
        self.assertIn('not a name this tool created', change.reason)

    def test_a_library_no_longer_in_device_conf_is_left_alone(self):
        self.saved['storage_objects'][0]['name'] = 'lib90_changer'
        self.assertFalse(self._plan()['lib90_changer'].changes)

    def test_a_drive_position_past_the_end_is_left_alone(self):
        self.saved['storage_objects'][0]['name'] = 'lib10_drive9'
        self.assertFalse(self._plan()['lib10_drive9'].changes)

    def test_other_plugins_are_not_touched(self):
        self.saved['storage_objects'].append(
            {'name': 'lib10_changer_disk', 'plugin': 'block', 'dev': '/dev/sdb'})
        self.assertNotIn('lib10_changer_disk', self._plan())

    def test_a_device_without_a_generic_node_cannot_be_used(self):
        devices = [ScsiDevice(address=ScsiAddress(16, 0, 0, 0),
                              device_type='mediumx', vendor='X', model='L700',
                              revision='1', device_path='/dev/sch1',
                              generic_path=None)] + AFTER_REBOOT[1:]
        self.assertTrue(self._plan(devices)['lib10_changer'].withhold)


class ApplyTests(TestCase):
    def test_only_the_dev_fields_change(self):
        saved = json.loads((FIXTURES / 'targetcli-saveconfig.json').read_text())
        conf = device_conf.parse((FIXTURES / 'device.conf').read_text())
        updated = remap.apply(saved, remap.plan(saved, conf, AFTER_REBOOT))

        before = {o['name']: o for o in saved['storage_objects']}
        after = {o['name']: o for o in updated['storage_objects']}
        self.assertEqual(after['lib10_changer']['dev'], '/dev/sg5')
        for name in before:
            self.assertEqual({k: v for k, v in before[name].items() if k != 'dev'},
                             {k: v for k, v in after[name].items() if k != 'dev'})
        self.assertEqual(saved['targets'], updated['targets'])

    def test_the_input_is_not_modified(self):
        saved = json.loads((FIXTURES / 'targetcli-saveconfig.json').read_text())
        conf = device_conf.parse((FIXTURES / 'device.conf').read_text())
        original = json.dumps(saved, sort_keys=True)
        remap.apply(saved, remap.plan(saved, conf, AFTER_REBOOT))
        self.assertEqual(json.dumps(saved, sort_keys=True), original)


class RemapTests(TestCase):
    """The whole operation, with sudo and discovery faked."""

    def setUp(self):
        import shutil
        import tempfile
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.saved = (FIXTURES / 'targetcli-saveconfig.json').read_text()
        self.writes = []

        def sudo(argv, **kwargs):
            self.writes.append(list(argv))
            return CommandResult(argv, 0, '', '')

        def sudo_tee(path, text, **kwargs):
            self.writes.append(['tee', str(path), text])
            return CommandResult(['tee'], 0, '', '')

        def sudo_cat(path, **kwargs):
            # saveconfig.json from the test; everything else - device.conf -
            # from the scratch directory, as the real function would read it.
            if str(path) == remap.SAVECONFIG:
                return CommandResult(['cat'], 0, self.saved, '')
            try:
                return CommandResult(['cat'], 0, Path(path).read_text(), '')
            except OSError as exc:
                return CommandResult(['cat'], 1, '', str(exc))

        for name, fake in (('sudo_cat', sudo_cat), ('sudo', sudo),
                           ('sudo_tee', sudo_tee)):
            patch = mock.patch.object(remap.shell, name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

        patch = mock.patch.object(remap.lsscsi, 'discover', return_value=AFTER_REBOOT)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_dry_run_writes_nothing(self):
        result = remap.remap(dry_run=True, config_directory=self.config)
        self.assertTrue(result.success)
        self.assertEqual(self.writes, [])
        self.assertIn('would be repointed', result.message)

    def test_the_file_is_backed_up_before_it_is_rewritten(self):
        result = remap.remap(config_directory=self.config)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(self.writes[0][:3], ['cp', '-p', remap.SAVECONFIG])
        self.assertEqual(self.writes[1][0], 'tee')
        written = json.loads(self.writes[1][2])
        devs = {o['name']: o['dev'] for o in written['storage_objects']}
        self.assertEqual(devs['lib10_changer'], '/dev/sg5')

    def test_nothing_to_change_writes_nothing(self):
        conf = device_conf.parse((FIXTURES / 'device.conf').read_text())
        saved = json.loads(self.saved)
        self.saved = json.dumps(remap.apply(saved, remap.plan(saved, conf, AFTER_REBOOT)))
        result = remap.remap(config_directory=self.config)
        self.assertTrue(result.success)
        self.assertEqual(self.writes, [])

    def test_a_failed_backup_stops_the_write(self):
        with mock.patch.object(remap.shell, 'sudo',
                               return_value=CommandResult(['cp'], 1, '', 'no space')):
            result = remap.remap(config_directory=self.config)
        self.assertFalse(result.success)
        self.assertFalse(any(w[0] == 'tee' for w in self.writes))

    def test_no_saved_configuration_is_not_an_error(self):
        """A host that has never exported anything has nothing to remap."""
        self.saved = None
        with mock.patch.object(remap.shell, 'sudo_cat',
                               side_effect=lambda path, **k: CommandResult(
                                   ['cat'], 1, '', 'missing')):
            result = remap.remap(config_directory=self.config)
        self.assertTrue(result.success)

    def test_an_unreadable_device_conf_is_a_failure(self):
        """Without it nothing can be resolved, and target.service should not
        restore a file this could not check."""
        import tempfile
        result = remap.remap(config_directory=tempfile.mkdtemp())
        self.assertFalse(result.success)

    def test_malformed_json_is_a_failure(self):
        self.saved = '{broken'
        self.assertFalse(remap.remap(config_directory=self.config).success)


class WaitTests(TestCase):
    """The 1.8 daemons add their units after mhvtl.target reports started."""

    def test_waits_until_every_declared_device_is_visible(self):
        conf = device_conf.parse(
            'Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00\n'
            'Drive: 11 CHANNEL: 00 TARGET: 01 LUN: 00\n'
            ' Library ID: 10 Slot: 01\n')
        partial, complete = AFTER_REBOOT[:1], AFTER_REBOOT[:2]
        with mock.patch.object(remap.lsscsi, 'discover',
                               side_effect=[partial, partial, complete]) as discover, \
             mock.patch.object(remap.time, 'sleep'):
            found = remap.wait_for_devices(conf, timeout=30)
        self.assertEqual(found, complete)
        self.assertEqual(discover.call_count, 3)

    def test_gives_up_at_the_deadline_with_what_it_has(self):
        conf = device_conf.parse('Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00\n')
        with mock.patch.object(remap.lsscsi, 'discover', return_value=[]), \
             mock.patch.object(remap.time, 'sleep'), \
             mock.patch.object(remap.time, 'monotonic', side_effect=[0, 0, 100]):
            self.assertEqual(remap.wait_for_devices(conf, timeout=10), [])


class UnitFileTests(TestCase):
    """The ordering is the whole design, so it is pinned."""

    def test_the_boot_unit_runs_after_mhvtl_and_before_the_target(self):
        unit = (Path(__file__).resolve().parents[3] / 'packaging' / 'systemd' /
                'mhvtl-iscsi-remap.service').read_text()
        self.assertIn('After=mhvtl.target', unit)
        self.assertIn('Before=target.service', unit)
        self.assertIn('RequiredBy=target.service', unit)
        self.assertIn('--wait', unit)


class WithholdTests(TestCase):
    """A backstore whose device is missing at boot comes out of the restore,
    with its LUNs and ACL mappings, and is recorded to be put back."""

    def setUp(self):
        self.saved = json.loads((FIXTURES / 'targetcli-saveconfig.json').read_text())
        self.conf = device_conf.parse((FIXTURES / 'device.conf').read_text())
        tpg = self.saved['targets'][0]['tpgs'][0]
        self.changer_lun = next(l['index'] for l in tpg['luns']
                                if l['storage_object'].endswith('/lib10_changer'))
        tpg.setdefault('node_acls', []).append({
            'node_wwn': 'iqn.2026-09.com.example:host',
            'mapped_luns': [{'index': 7, 'tpg_lun': self.changer_lun, 'write_protect': False}]})

    def test_the_backstore_its_luns_and_acl_mappings_come_out(self):
        changes = remap.plan(self.saved, self.conf, AFTER_REBOOT[1:])
        updated = remap.apply(self.saved, changes)
        names = [o['name'] for o in updated['storage_objects']]
        self.assertNotIn('lib10_changer', names)
        tpg = updated['targets'][0]['tpgs'][0]
        self.assertNotIn(self.changer_lun, [l['index'] for l in tpg['luns']])
        self.assertEqual(tpg['node_acls'][-1]['mapped_luns'], [])
        # the others are untouched
        self.assertEqual(len(names), len(self.saved['storage_objects']) - 1)

    def test_what_it_takes_to_put_it_back_is_recorded(self):
        changes = remap.plan(self.saved, self.conf, AFTER_REBOOT[1:])
        entries = remap.withheld_entries(self.saved, changes)
        entry = entries['lib10_changer']
        self.assertEqual(tuple(entry['address']), self.conf.address_of(10))
        self.assertEqual(entry['luns'][0]['index'], self.changer_lun)
        self.assertEqual(entry['luns'][0]['mapped'][0]['index'], 7)
