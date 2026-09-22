"""Mapping libraries and drives to their SCSI devices.

The service used to pick the Nth changer reported by lsscsi for the Nth
`Library:` line in device.conf. That is correct only while both happen to be in
the same order. device.conf is written by hand or by a generator, lsscsi orders
by SCSI address, and sg numbers are reassigned when the module reloads - so the
two agree by luck, not by rule. Both now match on the CHANNEL/TARGET/LUN that
device.conf gives each device.

The mapping moved to services/scsi/mapping.py in step 4. These tests called
it through the old tape operations adapter until that was removed; they call
it directly now, discovery faked at services/scsi/lsscsi, with the original
assertions.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.scsi import lsscsi, mapping
from apps.libraries.services.scsi.models import ScsiAddress, ScsiDevice

# device.conf written in library-id order, while the SCSI addresses are not in
# that order - the arrangement where position matching picks the wrong device.
DEVICE_CONF = """VERSION: 5

Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00
 Vendor identification: STK
 Product identification: L700

Drive: 11 CHANNEL: 00 TARGET: 01 LUN: 00
 Library ID: 10 Slot: 01

Library: 20 CHANNEL: 00 TARGET: 13 LUN: 00
 Vendor identification: SONY
 Product identification: LIB-302

Drive: 21 CHANNEL: 00 TARGET: 14 LUN: 00
 Library ID: 20 Slot: 01

Library: 30 CHANNEL: 00 TARGET: 08 LUN: 00
 Vendor identification: STK
 Product identification: L80

Drive: 31 CHANNEL: 00 TARGET: 09 LUN: 00
 Library ID: 30 Slot: 01
"""

def device(target, kind, vendor, model, revision, path, generic):
    return ScsiDevice(address=ScsiAddress(16, 0, target, 0), device_type=kind,
                      vendor=vendor, model=model, revision=revision,
                      device_path=path, generic_path=generic)


# lsscsi reports in SCSI-address order: targets 0, 8, 13.
ROBOTS = [
    device(0,  'mediumx', 'STK',  'L700',    '0108', '/dev/sch1', '/dev/sg4'),
    device(8,  'mediumx', 'STK',  'L80',     '0108', '/dev/sch0', '/dev/sg3'),
    device(13, 'mediumx', 'SONY', 'LIB-302', '0500', '/dev/sch2', '/dev/sg5'),
]
TAPES = [
    device(1,  'tape', 'IBM',  'ULT3580-TD8', '0108', '/dev/st0', '/dev/sg6'),
    device(9,  'tape', 'STK',  'T10000B',     '0108', '/dev/st1', '/dev/sg7'),
    device(14, 'tape', 'SONY', 'SDX-900V',    '0500', '/dev/st5', '/dev/sg11'),
]


class DeviceMappingTests(TestCase):
    def setUp(self):
        self.config = tempfile.mkdtemp()
        Path(self.config, 'device.conf').write_text(DEVICE_CONF)

    def library(self, library_id, config=None):
        return mapping.device_for_library(library_id, config_dir=config or self.config)

    def drive(self, drive_id):
        return mapping.device_for_drive(drive_id, config_dir=self.config)

    def _with_devices(self, devices=None):
        return mock.patch.object(
            lsscsi, 'discover',
            return_value=ROBOTS + TAPES if devices is None else devices)

    def test_library_maps_to_the_changer_at_its_scsi_address(self):
        with self._with_devices():
            self.assertEqual(self.library(10), '/dev/sg4')
            self.assertEqual(self.library(20), '/dev/sg5')
            self.assertEqual(self.library(30), '/dev/sg3')

    def test_position_matching_would_have_picked_the_wrong_changer(self):
        """Library 20 is second in device.conf but third by SCSI address.

        Choosing the second changer would hand back library 30's robot, and a
        move or unmount would act on the wrong library.
        """
        second_changer_by_position = ROBOTS[1].generic_path
        with self._with_devices():
            correct = self.library(20)

        self.assertEqual(second_changer_by_position, '/dev/sg3')
        self.assertNotEqual(correct, second_changer_by_position)
        self.assertEqual(correct, '/dev/sg5')

    def test_drive_maps_to_the_tape_device_at_its_scsi_address(self):
        with self._with_devices():
            self.assertEqual(self.drive(11), '/dev/st0')
            self.assertEqual(self.drive(21), '/dev/st5')
            self.assertEqual(self.drive(31), '/dev/st1')

    def test_unknown_library_returns_nothing(self):
        with self._with_devices():
            self.assertIsNone(self.library(99))

    def test_address_with_no_matching_device_returns_nothing(self):
        """Better to report no device than to operate on the wrong one."""
        with self._with_devices([ROBOTS[0]]):
            self.assertIsNone(self.library(20))

    def test_no_devices_at_all(self):
        with self._with_devices([]):
            self.assertIsNone(self.library(10))
            self.assertIsNone(self.drive(11))

    def test_missing_device_conf_is_survivable(self):
        with self._with_devices():
            self.assertIsNone(self.library(10, config=tempfile.mkdtemp()))


class ServicePathsFromSettingsTests(TestCase):
    """The old service hardcoded /etc/mhvtl and /opt/mhvtl, so settings were
    ignored and an instance without read access to /etc/mhvtl reported only
    "Could not find device for library N". services/core reads settings."""

    def test_defaults_come_from_settings(self):
        from apps.libraries.services.core import config_dir, home_dir
        with self.settings(MHVTL_CONFIG_DIR='/tmp/conf-x', MHVTL_HOME_DIR='/tmp/home-x'):
            self.assertEqual(str(config_dir()), '/tmp/conf-x')
            self.assertEqual(str(home_dir()), '/tmp/home-x')

    def test_explicit_arguments_still_win(self):
        from apps.libraries.services.tapes import TapeService
        service = TapeService('/tmp/explicit', '/tmp/media')
        self.assertEqual(str(service.config_dir), '/tmp/explicit')
        self.assertEqual(str(service.media_dir), '/tmp/media')
