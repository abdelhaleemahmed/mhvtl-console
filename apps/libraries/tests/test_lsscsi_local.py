"""lsscsi.local(): an iSCSI initiator's devices are not this host's libraries.

Logged into library 10's export on this host, the initiator shows its changer
at 17:0:0:0 - channel:target:lun 0:0:0, library 10's own address. Everything
that finds a library's device matches on that triple.
"""
from unittest import mock

from django.test import SimpleTestCase

from apps.libraries.services.scsi import lsscsi, mapping

LSSCSI = """\
[16:0:0:0]   mediumx STK      L700             0108  /dev/sch0  /dev/sg3
[16:0:1:0]   tape    IBM      ULT3580-TD8      0108  /dev/st3   /dev/sg9
[17:0:0:0]   mediumx STK      L700             0108  /dev/sch4  /dev/sg21
[17:0:0:1]   tape    IBM      ULT3580-TD8      0108  /dev/st14  /dev/sg22
"""
DRIVERS = {16: 'mhvtl', 17: 'iscsi_tcp'}


class LocalTests(SimpleTestCase):

    def setUp(self):
        self.devices = lsscsi.parse(LSSCSI)
        patch = mock.patch.object(lsscsi, 'host_driver', side_effect=DRIVERS.get)
        patch.start()
        self.addCleanup(patch.stop)

    def test_initiator_devices_are_left_out(self):
        kept = [d.generic_path for d in lsscsi.local(self.devices)]
        self.assertEqual(kept, ['/dev/sg3', '/dev/sg9'])

    def test_an_unknown_host_is_kept(self):
        with mock.patch.object(lsscsi, 'host_driver', return_value=None):
            self.assertEqual(len(lsscsi.local(self.devices)), 4)

    def test_library_10_is_found_on_its_own_host_whatever_lsscsi_lists_first(self):
        conf = mock.Mock(libraries={10: {}}, address_of=lambda i: (0, 0, 0))
        listed_backwards = list(reversed(self.devices))
        with mock.patch.object(mapping, '_load_conf', return_value=conf), \
             mock.patch.object(lsscsi, 'discover', return_value=listed_backwards):
            self.assertEqual(mapping.device_for_library(10), '/dev/sg3')
