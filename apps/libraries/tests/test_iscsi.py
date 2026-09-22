"""iSCSI: IQN and device validation, and the mutation path.

Step 7 of the service-layer refactor. targetcli is faked throughout - these
tests are about what reaches it, which is the part that was missing.

The device check matters most. create_block_backstore passed its device_path
straight to targetcli with no validation, so an operator typing /dev/sda
exported the system disk to any initiator that connected, and until recently
that endpoint was CSRF-exempt as well.
"""
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.iscsi import IscsiService, parsing, targetcli
from apps.libraries.services.scsi.models import ScsiAddress, ScsiDevice

FIXTURES = Path(__file__).parent / 'fixtures'


def ok(stdout=''):
    return CommandResult(['targetcli'], 0, stdout, '')


def failed(stderr='refused'):
    return CommandResult(['targetcli'], 1, '', stderr)


MHVTL_DEVICES = [
    ScsiDevice(address=ScsiAddress(16, 0, 0, 0), device_type='mediumx',
               vendor='STK', model='L700', revision='0108',
               device_path='/dev/sch1', generic_path='/dev/sg4'),
    ScsiDevice(address=ScsiAddress(16, 0, 1, 0), device_type='tape',
               vendor='IBM', model='ULT3580-TD8', revision='0108',
               device_path='/dev/st0', generic_path='/dev/sg6'),
]


class IqnValidationTests(TestCase):
    """The old check was iqn.startswith('iqn.')."""

    def test_accepts_a_well_formed_iqn(self):
        self.assertEqual(targetcli.validate_iqn('iqn.2026-01.com.example:library10'),
                         'iqn.2026-01.com.example:library10')

    def test_accepts_one_without_a_label(self):
        targetcli.validate_iqn('iqn.2026-01.com.example')

    def test_refuses_the_prefix_on_its_own(self):
        """'iqn.' passed the old check."""
        with self.assertRaises(targetcli.InvalidIqn):
            targetcli.validate_iqn('iqn.')

    def test_refuses_shell_fragments(self):
        for attempt in ('iqn.$(reboot)', 'iqn.2026-01.com.x; rm -rf /',
                        'iqn.2026-01.com.x`id`'):
            with self.subTest(iqn=attempt):
                with self.assertRaises(targetcli.InvalidIqn):
                    targetcli.validate_iqn(attempt)

    def test_refuses_a_missing_or_malformed_date(self):
        for attempt in ('iqn.com.example:x', 'iqn.26-1.com.example:x'):
            with self.subTest(iqn=attempt):
                with self.assertRaises(targetcli.InvalidIqn):
                    targetcli.validate_iqn(attempt)

    def test_refuses_empty(self):
        with self.assertRaises(targetcli.InvalidIqn):
            targetcli.validate_iqn('')


class DeviceValidationTests(TestCase):
    def setUp(self):
        patch = mock.patch.object(targetcli, 'mhvtl_devices',
                                  return_value=['/dev/sch1', '/dev/sg4',
                                                '/dev/st0', '/dev/sg6'])
        patch.start()
        self.addCleanup(patch.stop)

    def test_accepts_an_mhvtl_device(self):
        self.assertEqual(targetcli.validate_device('/dev/sg4'), '/dev/sg4')

    def test_refuses_the_system_disk(self):
        """The reason this check exists."""
        with self.assertRaises(targetcli.DeviceRefused) as refusal:
            targetcli.validate_device('/dev/sda')
        self.assertIn('not an MHVTL device', str(refusal.exception))

    def test_refuses_a_file_that_is_not_a_device(self):
        with self.assertRaises(targetcli.DeviceRefused):
            targetcli.validate_device('/etc/passwd')

    def test_refuses_a_device_node_this_host_does_not_have(self):
        """Right shape, wrong host: /dev/sg99 could be anything later."""
        with self.assertRaises(targetcli.DeviceRefused):
            targetcli.validate_device('/dev/sg99')

    def test_an_explicit_caller_can_override(self):
        """Exporting something else is a decision, not a typo."""
        self.assertEqual(targetcli.validate_device('/dev/sda', allow_any=True),
                         '/dev/sda')


class DeviceDiscoveryTests(TestCase):
    """The allowed list comes from what MHVTL actually created."""

    def test_device_list_is_read_from_discovery(self):
        with mock.patch('apps.libraries.services.scsi.lsscsi.discover',
                        return_value=MHVTL_DEVICES):
            self.assertEqual(targetcli.mhvtl_devices(),
                             ['/dev/sch1', '/dev/sg4', '/dev/sg6', '/dev/st0'])

    def test_no_devices_means_only_the_shape_is_checked(self):
        """A host where lsscsi answers nothing should not refuse every device;
        the node pattern still applies."""
        with mock.patch('apps.libraries.services.scsi.lsscsi.discover',
                        return_value=[]):
            self.assertEqual(targetcli.validate_device('/dev/sg4'), '/dev/sg4')
            with self.assertRaises(targetcli.DeviceRefused):
                targetcli.validate_device('/dev/sda')


class MutationTests(TestCase):
    """Nothing reaches targetcli until it has been checked."""

    def setUp(self):
        self.service = IscsiService()
        patch = mock.patch.object(targetcli, 'mhvtl_devices',
                                  return_value=['/dev/sg4', '/dev/st0'])
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_bad_iqn_never_reaches_targetcli(self):
        with mock.patch.object(targetcli, 'run') as run:
            result = self.service.create_target('iqn.')
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_a_bad_device_never_reaches_targetcli(self):
        with mock.patch.object(targetcli, 'run') as run:
            result = self.service.create_backstore('/dev/sda', 'rootdisk',
                                                   plugin='block')
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_a_good_target_is_created_and_saved(self):
        with mock.patch.object(targetcli, 'run', return_value=ok()) as run:
            result = self.service.create_target('iqn.2026-01.com.example:library10')
        self.assertTrue(result.success, result.message)
        commands = [call[0][0][0] for call in run.call_args_list]
        self.assertIn('/iscsi', commands)
        self.assertIn('saveconfig', commands)

    def test_a_refusal_from_targetcli_is_reported(self):
        with mock.patch.object(targetcli, 'run',
                               return_value=failed('IQN already exists')):
            result = self.service.create_target('iqn.2026-01.com.example:library10')
        self.assertFalse(result.success)
        self.assertIn('already exists', ' '.join(result.errors))

    def test_a_failed_save_is_a_warning_not_a_failure(self):
        """The change was made; it just will not survive a reboot."""
        def run(args, **kwargs):
            return failed('cannot write') if args[0] == 'saveconfig' else ok()

        with mock.patch.object(targetcli, 'run', side_effect=run):
            result = self.service.create_target('iqn.2026-01.com.example:library10')
        self.assertTrue(result.success)
        self.assertIn('saveconfig failed', result.data['warning'])

    def test_generate_node_acls_is_an_explicit_call(self):
        """Turning it on removes the access control on a target."""
        with mock.patch.object(targetcli, 'run', return_value=ok()) as run:
            result = self.service.set_generate_node_acls(
                'iqn.2026-01.com.example:library10', True)
        self.assertTrue(result.success)
        self.assertIn('Any initiator may now attach', result.message)

    def test_unknown_service_action_is_refused(self):
        self.assertFalse(self.service.service('destroy').success)


class ConfigParsingTests(TestCase):
    """saveconfig.json, against a real file from a host exporting library 10."""

    @classmethod
    def setUpTestData(cls):
        cls.text = (FIXTURES / 'targetcli-saveconfig.json').read_text()
        cls.parsed = parsing.parse_config(cls.text)

    def test_reads_the_target(self):
        targets = self.parsed['targets']
        self.assertEqual([t.iqn for t in targets],
                         ['iqn.2026-04.com.mhvtl:library10'])

    def test_reads_the_tpg_and_its_luns(self):
        tpg = self.parsed['targets'][0].tpgs[0]
        self.assertEqual(tpg.tag, 1)
        self.assertEqual(len(tpg.luns), 3)

    def test_a_lun_knows_its_backstore_and_plugin(self):
        """The LUN names its backstore by path: /backstores/pscsi/<name>."""
        lun = self.parsed['targets'][0].tpgs[0].luns[0]
        self.assertEqual(lun.backstore_plugin, 'pscsi')
        self.assertTrue(lun.backstore_name.startswith('lib10'))

    def test_generate_node_acls_is_read_as_a_boolean(self):
        """It is 1 in the JSON, and it decides whether anyone may attach."""
        self.assertIs(self.parsed['targets'][0].tpgs[0].generate_node_acls, True)

    def test_reads_the_portal(self):
        portals = self.parsed['targets'][0].tpgs[0].portals
        self.assertEqual([(p.ip_address, p.port) for p in portals],
                         [('0.0.0.0', 3260)])

    def test_reads_every_backstore_with_its_device(self):
        backstores = self.parsed['backstores']
        self.assertEqual(len(backstores), 3)
        self.assertTrue(all(b.plugin == 'pscsi' for b in backstores))
        self.assertTrue(all(b.device_path.startswith('/dev/sg') for b in backstores))

    def test_malformed_json_is_reported_not_raised(self):
        """This is read on a status page; a 500 tells an operator less."""
        parsed = parsing.parse_config('{not json')
        self.assertFalse(parsed['readable'])
        self.assertEqual(parsed['targets'], [])

    def test_a_non_iscsi_fabric_is_skipped(self):
        import json
        config = json.loads(self.text)
        config['targets'].append({'fabric': 'loopback', 'wwn': 'naa.500', 'tpgs': []})
        self.assertEqual(len(parsing.targets_from_config(config)), 1)


class LsParsingTests(TestCase):
    """The printed tree, used when saveconfig.json cannot be read."""

    @classmethod
    def setUpTestData(cls):
        cls.text = (FIXTURES / 'targetcli-ls.txt').read_text()

    def test_reads_the_target_from_the_tree(self):
        targets = parsing.targets_from_ls(self.text)
        self.assertEqual([t.iqn for t in targets],
                         ['iqn.2026-04.com.mhvtl:library10'])

    def test_the_tpg_tag_is_read_not_assumed(self):
        """The version this replaces hardcoded tag 1, so tpg2 came back as 1."""
        tree = ('  o- iqn.2026-01.com.example:x ... [TPGs: 1]\n'
                '    o- tpg2 ....................... [no-gen-acls]\n')
        self.assertEqual(parsing.targets_from_ls(tree)[0].tpgs[0].tag, 2)

    def test_gen_acls_is_read_from_the_tpg_line(self):
        targets = parsing.targets_from_ls(self.text)
        self.assertTrue(targets[0].tpgs[0].generate_node_acls)

    def test_no_gen_acls_is_not_read_as_gen_acls(self):
        tree = ('  o- iqn.2026-01.com.example:x ... [TPGs: 1]\n'
                '    o- tpg1 ....................... [no-gen-acls, no-auth]\n')
        self.assertFalse(parsing.targets_from_ls(tree)[0].tpgs[0].generate_node_acls)

    def test_the_target_count_line_does_not_create_a_phantom_tpg(self):
        targets = parsing.targets_from_ls(self.text)
        self.assertEqual(len(targets[0].tpgs), 1)

    def test_backstores_are_only_read_under_a_plugin(self):
        """Given a whole tree, the old parser reported targets and portals as
        backstores because it never left the last plugin's subtree."""
        backstores = parsing.backstores_from_ls(self.text)
        self.assertEqual(backstores, [], 'this host has no backstores loaded')

    def test_a_populated_plugin_subtree_is_read(self):
        tree = (
            '  o- backstores ........................ [...]\n'
            '  | o- block ............ [Storage Objects: 1]\n'
            '  | | o- disk0 ........... [/dev/sdb (10.0GiB)]\n'
            '  | o- pscsi ............ [Storage Objects: 1]\n'
            '  | | o- lib10_drive0 ............ [/dev/sg6]\n'
            '  o- iscsi ...................... [Targets: 1]\n'
            '  | o- iqn.2026-01.com.example:x ... [TPGs: 1]\n')
        found = parsing.backstores_from_ls(tree)
        self.assertEqual([(b.name, b.plugin) for b in found],
                         [('disk0', 'block'), ('lib10_drive0', 'pscsi')])


class ReadingSourceTests(TestCase):
    """The JSON is the authority; the tree is the fallback."""

    def setUp(self):
        self.service = IscsiService()
        self.json_text = (FIXTURES / 'targetcli-saveconfig.json').read_text()
        self.tree_text = (FIXTURES / 'targetcli-ls.txt').read_text()

    def test_the_json_is_read_first(self):
        from apps.libraries.services.iscsi import service as service_module
        with mock.patch.object(service_module.shell, 'sudo_cat',
                               return_value=ok(self.json_text)), \
             mock.patch.object(targetcli, 'ls') as ls:
            result = self.service.targets()

        self.assertEqual(result.data['source'], 'saveconfig.json')
        ls.assert_not_called()

    def test_an_unreadable_json_falls_back_to_the_tree(self):
        from apps.libraries.services.iscsi import service as service_module
        with mock.patch.object(service_module.shell, 'sudo_cat',
                               return_value=failed('No such file')), \
             mock.patch.object(targetcli, 'ls', return_value=ok(self.tree_text)):
            result = self.service.targets()

        self.assertEqual(result.data['source'], 'targetcli ls')
        self.assertEqual(result.data['count'], 1)

    def test_malformed_json_also_falls_back(self):
        """A truncated save is worse than no save: it parses to nothing."""
        from apps.libraries.services.iscsi import service as service_module
        with mock.patch.object(service_module.shell, 'sudo_cat',
                               return_value=ok('{trunc')), \
             mock.patch.object(targetcli, 'ls', return_value=ok(self.tree_text)):
            self.assertEqual(self.service.targets().data['source'], 'targetcli ls')

    def test_neither_readable_is_a_failure_not_an_empty_list(self):
        from apps.libraries.services.iscsi import service as service_module
        with mock.patch.object(service_module.shell, 'sudo_cat',
                               return_value=failed()), \
             mock.patch.object(targetcli, 'ls', return_value=failed('not found')):
            result = self.service.targets()

        self.assertFalse(result.success)
        self.assertIn('targetcli ls', ' '.join(result.errors))


class ServiceControlTests(TestCase):
    def setUp(self):
        self.service = IscsiService()

    def _units(self):
        from apps.libraries.services.iscsi import service as service_module
        return service_module.units

    def test_a_known_action_reaches_systemctl(self):
        with mock.patch.object(self._units(), 'restart', return_value=ok()) as restart:
            result = self.service.service('restart')
        self.assertTrue(result.success)
        restart.assert_called_once_with('target.service')

    def test_an_unknown_action_is_refused_before_systemctl(self):
        """This is reachable from a web form; `mask` is not a thing to offer."""
        with mock.patch.object(self._units(), '_control') as control:
            result = self.service.service('mask')
        self.assertFalse(result.success)
        control.assert_not_called()

    def test_a_refusal_is_reported(self):
        with mock.patch.object(self._units(), 'stop', return_value=failed('is masked')):
            result = self.service.service('stop')
        self.assertFalse(result.success)
        self.assertIn('masked', ' '.join(result.errors))


class ExportWorkflowTests(TestCase):
    """Exporting a library: a sequence, not an operation.

    Moved out of iscsi_service.py in step 8. Every step is faked at the service
    level; what these test is the order, the LUN numbering and what happens when
    one step of several fails.
    """

    DEVICES = [
        {'name': 'lib10_drive0', 'device_path': '/dev/sg6', 'type': 'tape'},
        {'name': 'lib10_changer', 'device_path': '/dev/sg4', 'type': 'changer'},
        {'name': 'lib10_drive1', 'device_path': '/dev/sg7', 'type': 'tape'},
    ]

    def setUp(self):
        from apps.libraries.services.iscsi import workflow
        self.workflow = workflow
        self.service = IscsiService()

    def _all_ok(self):
        from apps.libraries.services.core import success_result
        return [
            mock.patch.object(IscsiService, 'create_target',
                              return_value=success_result('target created')),
            mock.patch.object(IscsiService, 'create_backstore',
                              return_value=success_result('backstore created')),
            mock.patch.object(IscsiService, 'create_lun',
                              return_value=success_result('lun created')),
            mock.patch.object(IscsiService, 'set_generate_node_acls',
                              return_value=success_result('open')),
            mock.patch.object(IscsiService, 'create_acl',
                              return_value=success_result('acl created')),
        ]

    def _run(self, **kwargs):
        for patch in self._all_ok():
            patch.start()
            self.addCleanup(patch.stop)
        return self.workflow.export_library(10, self.DEVICES,
                                            service=self.service, **kwargs)

    def test_the_changer_is_exported_as_lun_zero(self):
        """Some backup software stops scanning at the first device it does not
        recognise, so a changer behind two drives is a library with no robot."""
        result = self._run()
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.data['luns'][0]['device_path'], '/dev/sg4')
        self.assertEqual(result.data['luns'][0]['lun_id'], 0)

    def test_every_device_gets_a_lun(self):
        result = self._run()
        self.assertEqual(len(result.data['luns']), 3)
        self.assertEqual([lun['lun_id'] for lun in result.data['luns']], [0, 1, 2])

    def test_the_iqn_is_generated_from_the_library_id(self):
        result = self._run()
        self.assertTrue(result.data['iqn'].endswith(':library10'))
        self.assertTrue(result.data['iqn'].startswith('iqn.'))

    def test_an_explicit_iqn_is_used(self):
        result = self._run(iqn='iqn.2026-01.com.example:mine')
        self.assertEqual(result.data['iqn'], 'iqn.2026-01.com.example:mine')

    def test_the_steps_are_recorded_in_order(self):
        result = self._run()
        steps = [entry['step'] for entry in result.data['steps']]
        self.assertEqual(steps[0], 'create target')
        self.assertIn('backstore lib10_changer', steps)
        self.assertEqual(steps[-1], 'allow any initiator')

    def test_a_failed_target_stops_everything(self):
        from apps.libraries.services.core import failure_result, success_result
        with mock.patch.object(IscsiService, 'create_target',
                               return_value=failure_result('exists', ['IQN in use'])), \
             mock.patch.object(IscsiService, 'create_backstore') as backstore:
            result = self.workflow.export_library(10, self.DEVICES,
                                                  service=self.service)
        self.assertFalse(result.success)
        backstore.assert_not_called()

    def test_one_failed_device_does_not_stop_the_others(self):
        """A half-exported library wants the missing drive added, not a restart."""
        from apps.libraries.services.core import failure_result, success_result

        def backstore(device_path, name, **kwargs):
            return (failure_result('busy', ['device in use'])
                    if device_path == '/dev/sg7' else success_result('created'))

        with mock.patch.object(IscsiService, 'create_target',
                               return_value=success_result('created')), \
             mock.patch.object(IscsiService, 'create_backstore',
                               side_effect=backstore), \
             mock.patch.object(IscsiService, 'create_lun',
                               return_value=success_result('created')), \
             mock.patch.object(IscsiService, 'set_generate_node_acls',
                               return_value=success_result('open')):
            result = self.workflow.export_library(10, self.DEVICES,
                                                  service=self.service)

        self.assertTrue(result.success)
        self.assertEqual(len(result.data['luns']), 2)
        self.assertTrue(any('/dev/sg7' in w or 'lib10_drive1' in w
                            for w in result.data['warnings']))
        self.assertIn('step(s) failed', result.message)

    def test_a_target_that_exports_nothing_is_a_failure(self):
        from apps.libraries.services.core import failure_result, success_result
        with mock.patch.object(IscsiService, 'create_target',
                               return_value=success_result('created')), \
             mock.patch.object(IscsiService, 'create_backstore',
                               return_value=failure_result('no', ['refused'])), \
             mock.patch.object(IscsiService, 'set_generate_node_acls',
                               return_value=success_result('open')):
            result = self.workflow.export_library(10, self.DEVICES,
                                                  service=self.service)

        self.assertFalse(result.success)
        self.assertIn('exports no devices', ' '.join(result.errors))

    def test_a_named_initiator_gets_an_acl_and_the_target_stays_closed(self):
        for patch in self._all_ok():
            patch.start()
            self.addCleanup(patch.stop)

        from apps.libraries.services.core import success_result

        with mock.patch.object(IscsiService, 'set_generate_node_acls',
                               return_value=success_result('closed')) as acls:
            result = self.workflow.export_library(
                10, self.DEVICES, allow_all_initiators=False,
                initiator_iqn='iqn.2026-01.com.example:backup01',
                service=self.service)

        self.assertFalse(acls.call_args[0][1], 'generate_node_acls must be off')
        self.assertTrue(any('backup01' in entry['step']
                            for entry in result.data['steps']))

    def test_requiring_acls_with_no_initiator_warns_that_nothing_can_attach(self):
        for patch in self._all_ok():
            patch.start()
            self.addCleanup(patch.stop)

        result = self.workflow.export_library(10, self.DEVICES,
                                              allow_all_initiators=False,
                                              service=self.service)
        self.assertTrue(any('no initiator can attach' in w
                            for w in result.data['warnings']))

    def test_a_device_with_no_path_is_reported_not_skipped_silently(self):
        for patch in self._all_ok():
            patch.start()
            self.addCleanup(patch.stop)

        result = self.workflow.export_library(
            10, [{'name': 'broken'}, {'device_path': '/dev/sg4', 'type': 'changer'}],
            service=self.service)
        self.assertTrue(any('no device_path' in entry['detail']
                            for entry in result.data['steps']))


class ServiceSurfaceTests(TestCase):
    """What iscsi_views.py relies on, now that it calls services directly.

    These were the old adapter's tests (adapters/iscsi_service.py, removed);
    each case still holds, against services/iscsi and the view helpers.
    """

    def setUp(self):
        self.service = IscsiService()

    def test_a_bad_iqn_is_refused_before_targetcli(self):
        """The old check was iqn.startswith('iqn.')."""
        with mock.patch.object(targetcli, 'run') as run:
            result = self.service.create_target('iqn.')
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_the_system_disk_is_refused(self):
        with mock.patch.object(targetcli, 'mhvtl_devices',
                               return_value=['/dev/sg4']), \
             mock.patch.object(targetcli, 'run') as run:
            result = self.service.create_backstore('/dev/sda', 'rootdisk',
                                                   plugin='block')
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_service_control_maps_to_the_unit(self):
        from apps.libraries.services.console import units
        with mock.patch.object(units, 'restart', return_value=ok()) as restart:
            self.assertTrue(self.service.service('restart').success)
        restart.assert_called_once_with('target.service')

    def test_save_and_restore_report_targetcli_failures(self):
        with mock.patch.object(targetcli, 'save_config',
                               return_value=CommandResult([], 1, '', 'denied')):
            saved = self.service.save_config()
        self.assertFalse(saved.success)
        self.assertIn('denied', saved.errors[0])
        with mock.patch.object(targetcli, 'restore_config', return_value=ok('done')):
            restored = self.service.restore_config()
        self.assertTrue(restored.success)

    def _devices(self, devices):
        from apps.libraries.services.scsi import lsscsi
        return mock.patch.object(lsscsi, 'discover', return_value=devices)

    def test_available_devices_only_offers_ones_with_a_generic_node(self):
        """A pscsi backstore needs /dev/sgN; a device without one cannot be
        exported, so offering it produces a failure at create time."""
        devices = [
            ScsiDevice(address=ScsiAddress(16, 0, 0, 0), device_type='mediumx',
                       vendor='STK', model='L700', revision='0108',
                       device_path='/dev/sch1', generic_path='/dev/sg4'),
            ScsiDevice(address=ScsiAddress(16, 0, 2, 0), device_type='tape',
                       vendor='IBM', model='ULT3580-TD8', revision='0108',
                       device_path='/dev/st9', generic_path=None),
            ScsiDevice(address=ScsiAddress(0, 0, 0, 0), device_type='disk',
                       vendor='ATA', model='Samsung', revision='1.0',
                       device_path='/dev/sda', generic_path='/dev/sg0'),
        ]
        with self._devices(devices):
            offered = self.service.available_devices().data['devices']

        self.assertEqual([d['device_path'] for d in offered], ['/dev/sch1'])
        self.assertEqual(offered[0]['type'], 'changer')

    def test_the_system_disk_is_never_offered_for_export(self):
        disk = ScsiDevice(address=ScsiAddress(0, 0, 0, 0), device_type='disk',
                          vendor='ATA', model='Samsung', revision='1.0',
                          device_path='/dev/sda', generic_path='/dev/sg0')
        with self._devices([disk]):
            self.assertEqual(self.service.available_devices().data['devices'], [])

    def test_the_export_form_gets_its_device_types_filled_in(self):
        """The form posts paths and names but not types, and the workflow needs
        the type to export the changer as LUN 0."""
        from apps.libraries.services.core import success_result
        available = success_result('2', {'devices': [
            {'device_path': '/dev/sch1', 'generic_path': '/dev/sg4', 'type': 'changer'},
            {'device_path': '/dev/st0', 'generic_path': '/dev/sg6', 'type': 'tape'}]})
        with mock.patch.object(IscsiService, 'available_devices',
                               return_value=available):
            filled = self.service._with_device_types(
                [{'device_path': '/dev/st0'}, {'device_path': '/dev/sch1'}])
        self.assertEqual([d['type'] for d in filled], ['tape', 'changer'])

    def test_export_passes_the_filled_types_to_the_workflow(self):
        from apps.libraries.services.core import success_result
        from apps.libraries.services.iscsi import workflow
        available = success_result('1', {'devices': [
            {'device_path': '/dev/sch1', 'generic_path': '/dev/sg4', 'type': 'changer'}]})
        with mock.patch.object(IscsiService, 'available_devices',
                               return_value=available), \
             mock.patch.object(workflow, 'export_library',
                               return_value=success_result('ok')) as export:
            self.service.export_library(10, [{'device_path': '/dev/sch1'}])
        self.assertEqual(export.call_args.args[1][0]['type'], 'changer')

    def test_an_unknown_device_defaults_to_tape_not_changer(self):
        """Guessing changer would put an unknown device at LUN 0."""
        from apps.libraries.services.core import failure_result
        with mock.patch.object(IscsiService, 'available_devices',
                               return_value=failure_result('no lsscsi')):
            filled = self.service._with_device_types([{'device_path': '/dev/sg9'}])
        self.assertEqual(filled[0]['type'], 'tape')

    def test_the_dashboard_survives_a_host_with_no_iscsi_at_all(self):
        from apps.libraries import iscsi_views
        with mock.patch.object(IscsiService, '_configuration',
                               side_effect=RuntimeError('targetcli missing')):
            status = iscsi_views._status(self.service)
        self.assertFalse(status['service']['running'])
        self.assertEqual(status['targets'], [])
        self.assertEqual(status['service']['active_state'], 'inactive')

    def test_the_dashboard_shape(self):
        """The keys dashboard.html and the status poll read."""
        from apps.libraries import iscsi_views
        from apps.libraries.services.core import success_result
        answer = success_result('ok', {
            'service': {'running': True, 'enabled': True},
            'targets': [{'iqn': 'iqn.2026-01.com.example:library10'}],
            'backstores': [], 'config_saved': True})
        with mock.patch.object(IscsiService, 'status', return_value=answer):
            status = iscsi_views._status(self.service)
        self.assertEqual(status['service']['sub_state'], 'running')
        self.assertEqual(status['target_count'], 1)
        self.assertTrue(status['config_saved'])
        self.assertIn('last_checked', status)

    def test_a_target_is_found_by_iqn(self):
        from apps.libraries import iscsi_views
        from apps.libraries.services.core import success_result
        listed = success_result('2', {'targets': [{'iqn': 'iqn.a:x'}, {'iqn': 'iqn.a:y'}]})
        with mock.patch.object(IscsiService, 'targets', return_value=listed):
            found = iscsi_views._target(self.service, 'iqn.a:y')
            self.assertIsNone(iscsi_views._target(self.service, 'iqn.a:z'))
        self.assertEqual(found['iqn'], 'iqn.a:y')
        # Every target is told which library it exports, so the page can offer
        # to remove that library's devices with it. This one exports none.
        self.assertIsNone(found['library_id'])

    def test_the_export_page_maps_libraries_to_scsi_targets(self):
        from apps.libraries import iscsi_views
        from apps.libraries.services.config import device_conf
        from apps.libraries.services.config.service import ConfigService
        conf = device_conf.parse((Path(__file__).parent / 'fixtures'
                                  / 'device.conf').read_text())
        with mock.patch.object(ConfigService, 'device_conf', return_value=conf):
            mapping = iscsi_views._library_scsi_targets()
        self.assertEqual(mapping[10], [0, 1, 2, 3, 4])

    def test_the_lun_endpoint_passes_the_tpg_as_the_tpg(self):
        """It passed (tpg, lun_id) positionally where the adapter took
        (lun_id, tpg), so the TPG number went in as the LUN."""
        import json
        from django.test import RequestFactory
        from apps.libraries import iscsi_views
        from apps.libraries.services.core import success_result

        request = RequestFactory().post(
            '/', data=json.dumps({'iqn': 'iqn.a:x', 'backstore_plugin': 'pscsi',
                                  'backstore_name': 'lib10_changer', 'tpg': 2,
                                  'lun_id': 7}),
            content_type='application/json')
        request.session = {'mhvtl_logged_in': True}
        with mock.patch.object(IscsiService, 'create_lun',
                               return_value=success_result('ok')) as create:
            iscsi_views.AddLunAjaxView.as_view()(request)
        create.assert_called_once_with('iqn.a:x', 'pscsi', 'lib10_changer', tpg=2, lun=7)

    def test_the_status_poll_needs_a_login(self):
        from django.test import RequestFactory
        from apps.libraries import iscsi_views
        request = RequestFactory().get('/')
        request.session = {}
        response = iscsi_views.IscsiStatusAjaxView.as_view()(request)
        self.assertEqual(response.status_code, 401)
