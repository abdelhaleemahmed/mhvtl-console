"""ch_policy: what keeps the kernel ch driver off the changers.

The ch driver takes a reference on every drive a changer names by SCSI id and
never releases it; see services/scsi/ch_policy.py. These read fixtures only.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.libraries.services.core import CommandResult
from apps.libraries.services.scsi import ch_policy, lsscsi, mapping

LOG = """\
[40503.723941] ch 16:0:23:0: [ch4] type #1 (mt): 0x1+1 [medium transport]
[40503.726237] ch 16:0:23:0: [ch4] ID 24, LUN 0,
[40503.726247] ch 16:0:23:0: [ch4] Huh? device not found!
[40509.839173] ch 16:0:23:0: [ch4] type #1 (mt): 0x1+1 [medium transport]
[40509.841458] ch 16:0:23:0: [ch4] ID 24, LUN 0,
[40509.841488] ch 16:0:23:0: [ch4] name: IBM      ULT3580-HHA      D.02
[26371.470085] ch 16:0:0:0: [ch0] type #1 (mt): 0x1+1 [medium transport]
[26371.470085] ch 16:0:0:0: [ch0] ID/LUN unknown
"""
LSSCSI = """\
[16:0:0:0]   mediumx STK      L700             0108  /dev/sch0  /dev/sg4
[16:0:23:0]  mediumx IBM      3573-TL          D.02  /dev/sch4  /dev/sg23
"""


class ProbeTests(SimpleTestCase):

    def test_a_changer_that_names_its_drives_is_found_in_the_log(self):
        self.assertEqual(ch_policy.probe_reports(LOG),
                         {'16:0:23:0': True, '16:0:0:0': False})


class RuleTests(SimpleTestCase):

    def test_blacklists_and_udev_rules_are_listed_with_their_file_and_line(self):
        with tempfile.TemporaryDirectory() as root:
            modprobe = Path(root, 'modprobe.d'); modprobe.mkdir()
            (modprobe / 'mhvtl-no-ch.conf').write_text('# why\nblacklist ch\n')
            (modprobe / 'other.conf').write_text('blacklist chx\noptions sg x=1\n')
            udev = Path(root, 'rules.d'); udev.mkdir()
            (udev / '90-mhvtl.rules').write_text('ACTION=="add", DRIVER=="ch", RUN+="x"\n')
            with mock.patch.object(ch_policy, 'MODPROBE_DIRS', (str(modprobe),)), \
                 mock.patch.object(ch_policy, 'UDEV_DIRS', (str(udev),)):
                self.assertEqual([r['text'] for r in ch_policy.modprobe_rules()],
                                 ['blacklist ch'])
                self.assertEqual(ch_policy.modprobe_rules()[0]['line'], 2)
                self.assertEqual(len(ch_policy.udev_rules()), 1)


class StatusTests(SimpleTestCase):

    def status(self, *, rules, loaded, bound):
        with mock.patch.object(ch_policy, 'modprobe_rules', return_value=rules), \
             mock.patch.object(ch_policy, 'udev_rules', return_value=[]), \
             mock.patch.object(ch_policy, 'cmdline_rules', return_value=[]), \
             mock.patch.object(ch_policy, 'SYS_MODULE', mock.Mock(exists=lambda: loaded)), \
             mock.patch.object(ch_policy.shell, 'sudo',
                               return_value=CommandResult([], 0, LOG, '')), \
             mock.patch.object(lsscsi, 'local', return_value=lsscsi.parse(LSSCSI)), \
             mock.patch.object(ch_policy, 'bound_driver',
                               side_effect=lambda a: 'ch' if bound else ''):
            return ch_policy.status().data

    def test_no_rule_and_a_changer_naming_its_drives_is_exposed(self):
        data = self.status(rules=[], loaded=True, bound=True)
        self.assertEqual(data['state'], 'exposed')
        self.assertEqual([c['address'] for c in data['at_risk']], ['16:0:23:0'])
        self.assertEqual(data['active'], [])

    def test_a_blacklist_with_the_module_still_loaded_is_pending(self):
        rule = {'file': '/etc/modprobe.d/mhvtl-no-ch.conf', 'line': 2, 'text': 'blacklist ch'}
        self.assertEqual(self.status(rules=[rule], loaded=True, bound=True)['state'],
                         'pending')

    def test_a_blacklist_and_no_module_is_protected(self):
        rule = {'file': 'f', 'line': 1, 'text': 'blacklist ch'}
        data = self.status(rules=[rule], loaded=False, bound=False)
        self.assertEqual(data['state'], 'protected')
        self.assertEqual(data['at_risk'], [])


class ChangerNodeTests(SimpleTestCase):
    """One setting chooses the node, so ch can come back without code changes."""

    def setUp(self):
        self.device = lsscsi.parse(LSSCSI)[0]

    def test_the_generic_node_by_default(self):
        self.assertEqual(mapping.changer_node(self.device), '/dev/sg4')

    @override_settings(MHVTL_CHANGER_NODE='ch')
    def test_the_ch_node_when_asked_for(self):
        self.assertEqual(mapping.changer_node(self.device), '/dev/sch0')

    @override_settings(MHVTL_CHANGER_NODE='ch')
    def test_the_generic_node_when_ch_is_not_bound(self):
        device = lsscsi.parse('[16:0:0:0]   mediumx STK      L700   0108  -  /dev/sg4\n')[0]
        self.assertEqual(mapping.changer_node(device), '/dev/sg4')
