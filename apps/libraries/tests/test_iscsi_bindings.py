"""iscsi/bindings.py: noticing and repairing exports left on deleted devices.

Everything that would reach the host - targetcli, systemctl, configfs, the
state file - is replaced by fixtures, and the suite-wide MHVTL_ISCSI_REBIND
switch stays off except around mocked commands.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from apps.libraries import iscsi_views
from apps.libraries.services.core import CommandResult, success_result
from apps.libraries.services.iscsi import bindings
from apps.libraries.services.iscsi.service import IscsiService
from apps.libraries.tests.base import LibraryTestBase

IQN = 'iqn.2026-09.com.mhvtl:library70'


class Conf:
    """Library 70: changer at 0:0:0, drives 71 and 72 at 0:1:0 and 0:2:0."""
    libraries = {70: {}}

    def address_of(self, device_id):
        return {70: (0, 0, 0), 71: (0, 1, 0), 72: (0, 2, 0)}.get(device_id)

    def drives_of(self, library_id):
        return {71: {}, 72: {}} if library_id == 70 else {}


def device(ctl, node):
    return SimpleNamespace(address=SimpleNamespace(ctl=ctl), generic_path=node)


def config(*names_devs, acl=None):
    """A running configuration exporting the given backstores as LUN 0, 1, ..."""
    objects = [{'plugin': 'pscsi', 'name': name, 'dev': dev} for name, dev in names_devs]
    luns = [{'storage_object': f'/backstores/pscsi/{name}', 'index': index}
            for index, (name, _) in enumerate(names_devs)]
    acls = []
    if acl:
        acls = [{'node_wwn': acl, 'mapped_luns': [
            {'index': 5, 'tpg_lun': 0, 'write_protect': True}]}]
    return {'storage_objects': objects,
            'targets': [{'fabric': 'iscsi', 'wwn': IQN,
                         'tpgs': [{'tag': 1, 'luns': luns, 'node_acls': acls}]}]}


INVOCATIONS = {'vtllibrary@70.service': 'aaa', 'vtltape@71.service': 'bbb',
               'vtltape@72.service': 'ccc'}


def assess(cfg, devices, state=None, addresses=None):
    addresses = addresses or {'lib70_changer': (0, 0, 0), 'lib70_drive0': (0, 1, 0),
                              'STK_t10000b_2': (0, 2, 0)}
    return {b.name: b for b in bindings.assess(cfg, addresses, Conf(), devices,
                                               INVOCATIONS, state or {})}


class ReadingTests(SimpleTestCase):

    def test_the_bound_address_is_read_from_configfs_info(self):
        text = ('/sys/kernel/config/target/core/pscsi_2/lib10_changer/info:'
                '        SCSI Device Bus Location: Channel ID: 0 Target ID: 13 '
                'LUN: 0 Host ID: 16')
        self.assertEqual(bindings.parse_info(text), {'lib10_changer': (0, 13, 0)})

    def test_luns_and_their_acl_mappings_are_found_per_backstore(self):
        users = bindings.lun_users(config(('lib70_changer', '/dev/sg3'),
                                          acl='iqn.2026-09.com.example:host'))
        self.assertEqual(users['lib70_changer'], [{
            'fabric': 'iscsi', 'wwn': IQN, 'tpg': 1, 'index': 0,
            'mapped': [{'node_wwn': 'iqn.2026-09.com.example:host', 'index': 5,
                        'write_protect': True}]}])


class JudgingTests(SimpleTestCase):

    def test_a_backstore_on_a_node_that_now_belongs_to_another_device_is_stale(self):
        found = assess(config(('lib70_changer', '/dev/sg6')),
                       [device((0, 0, 0), '/dev/sg3')])
        self.assertEqual(found['lib70_changer'].status, bindings.STALE)
        self.assertIn('/dev/sg3', found['lib70_changer'].reason)

    def test_the_same_node_after_a_restart_is_stale_by_invocation(self):
        """The case nothing in configfs shows: same address, same node."""
        state = {'lib70_changer': {'unit': 'vtllibrary@70.service', 'invocation': 'old'}}
        found = assess(config(('lib70_changer', '/dev/sg3')),
                       [device((0, 0, 0), '/dev/sg3')], state)
        self.assertEqual(found['lib70_changer'].status, bindings.STALE)
        self.assertIn('restarted', found['lib70_changer'].reason)

    def test_a_recorded_binding_to_the_running_daemon_is_bound(self):
        state = {'lib70_changer': {'unit': 'vtllibrary@70.service', 'invocation': 'aaa'}}
        found = assess(config(('lib70_changer', '/dev/sg3')),
                       [device((0, 0, 0), '/dev/sg3')], state)
        self.assertEqual(found['lib70_changer'].status, bindings.BOUND)

    def test_no_record_is_unknown_not_bound(self):
        found = assess(config(('lib70_changer', '/dev/sg3')),
                       [device((0, 0, 0), '/dev/sg3')])
        self.assertEqual(found['lib70_changer'].status, bindings.UNKNOWN)

    def test_nothing_at_the_address_is_missing(self):
        found = assess(config(('lib70_drive0', '/dev/sg9')), [])
        self.assertEqual(found['lib70_drive0'].status, bindings.MISSING)

    def test_ownership_comes_from_the_address_not_the_name(self):
        """The Export Library form names backstores after the vendor and model."""
        found = assess(config(('STK_t10000b_2', '/dev/sg7')),
                       [device((0, 2, 0), '/dev/sg7')])
        self.assertEqual(found['STK_t10000b_2'].library_id, 70)
        self.assertEqual(found['STK_t10000b_2'].unit, 'vtltape@72.service')

    def test_a_device_config_does_not_declare_is_left_alone(self):
        found = assess(config(('other', '/dev/sg1')), [device((0, 9, 0), '/dev/sg1')],
                       addresses={'other': (0, 9, 0)})
        self.assertIsNone(found['other'].library_id)
        self.assertEqual(found['other'].status, bindings.UNKNOWN)


@override_settings(MHVTL_ISCSI_REBIND=True)
class RebindTests(SimpleTestCase):

    def setUp(self):
        self.commands = []

        def run(args, **kwargs):
            self.commands.append(list(args))
            return CommandResult(['targetcli', *args], 0, '', '')

        patches = [mock.patch.object(bindings.targetcli, 'run', side_effect=run),
                   mock.patch.object(bindings.targetcli, 'validate_device'),
                   mock.patch.object(bindings.targetcli, 'save_config',
                                     return_value=CommandResult([], 0, '', '')),
                   mock.patch.object(bindings, 'record')]
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def gathered(self, cfg, devices, state=None):
        found = list(assess(cfg, devices, state).values())
        return mock.patch.object(bindings, '_gather',
                                 return_value=((cfg, Conf(), found, state or {}), ''))

    def test_a_stale_backstore_is_recreated_with_its_lun_index_and_acl_mapping(self):
        cfg = config(('lib70_changer', '/dev/sg6'), acl='iqn.2026-09.com.example:host')
        with self.gathered(cfg, [device((0, 0, 0), '/dev/sg3')]):
            result = bindings.rebind(70, wait=0)

        self.assertTrue(result.success, result.errors)
        self.assertEqual(self.commands, [
            ['/backstores/pscsi', 'delete', 'lib70_changer'],
            ['/backstores/pscsi', 'create', 'name=lib70_changer', 'dev=/dev/sg3'],
            [f'/iscsi/{IQN}/tpg1/luns', 'create', '/backstores/pscsi/lib70_changer',
             'lun=0', 'add_mapped_luns=false'],
            [f'/iscsi/{IQN}/tpg1/acls/iqn.2026-09.com.example:host', 'create',
             'mapped_lun=5', 'tpg_lun_or_backstore=0', 'write_protect=1'],
        ])
        bindings.record.assert_called_once_with(['lib70_changer'], config_directory=None)

    def test_a_bound_backstore_is_left_alone(self):
        state = {'lib70_changer': {'unit': 'vtllibrary@70.service', 'invocation': 'aaa'}}
        with self.gathered(config(('lib70_changer', '/dev/sg3')),
                           [device((0, 0, 0), '/dev/sg3')], state):
            result = bindings.rebind(70, wait=0)
        self.assertTrue(result.success)
        self.assertEqual(self.commands, [])

    def test_a_missing_device_is_reported_and_not_pointed_elsewhere(self):
        with self.gathered(config(('lib70_drive0', '/dev/sg9')), []):
            result = bindings.rebind(70, wait=0)
        self.assertEqual(self.commands, [])
        self.assertEqual(result.data['left'][0]['name'], 'lib70_drive0')

    def test_other_libraries_are_not_touched(self):
        with self.gathered(config(('lib70_changer', '/dev/sg6')),
                           [device((0, 0, 0), '/dev/sg3')]):
            result = bindings.rebind(10, wait=0)
        self.assertEqual(self.commands, [])
        self.assertIn('no exported devices', result.message)

    def test_a_failed_step_stops_that_backstore_and_is_reported(self):
        bindings.targetcli.run.side_effect = lambda args, **kw: CommandResult(
            args, 1, '', 'storage object in use')
        with self.gathered(config(('lib70_changer', '/dev/sg6')),
                           [device((0, 0, 0), '/dev/sg3')]):
            result = bindings.rebind(70, wait=0)
        self.assertFalse(result.success)
        self.assertIn('in use', ' '.join(result.errors))


class GuardTests(SimpleTestCase):
    """The suite runs with MHVTL_ISCSI_REBIND off."""

    def test_rebind_refuses_under_the_test_settings(self):
        with self.assertRaises(bindings.RebindDisabled):
            bindings.rebind(10)

    def test_record_refuses_under_the_test_settings(self):
        with self.assertRaises(bindings.RebindDisabled):
            bindings.record()

    def test_after_restart_does_nothing_under_the_test_settings(self):
        with mock.patch.object(bindings, 'rebind') as rebind:
            self.assertEqual(bindings.after_restart(10), '')
        rebind.assert_not_called()

    @override_settings(MHVTL_ISCSI_REBIND=True)
    def test_after_restart_ignores_a_directory_that_is_not_the_live_one(self):
        with mock.patch.object(bindings, 'rebind') as rebind:
            self.assertEqual(bindings.after_restart(10, config_directory='/tmp/scratch'), '')
        rebind.assert_not_called()


class RestartHookTests(SimpleTestCase):
    """Restarting a library from the GUI rebinds what it exports."""

    def test_restart_services_rebinds_the_library_it_restarted(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import service as library_service
        with mock.patch.object(units, 'restart_library',
                               return_value={'ok': True, 'error': None,
                                             'restarted': 'vtllibrary@70.service'}), \
             mock.patch.object(library_service.iscsi_bindings, 'after_restart',
                               return_value='Library 70: rebound 1 exported device(s)') as hook:
            result = library_service.LibraryService('/tmp/unused').restart_services(70)
        self.assertEqual(hook.call_args.args, (70,))
        self.assertIn('rebound 1', result.message)


class DashboardTests(LibraryTestBase):
    """The iSCSI dashboard shows stale exports and offers to rebind them."""

    def setUp(self):
        super().setUp()
        self.login_as_user()
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()
        for patch in (mock.patch.object(iscsi_views, 'ISCSI_SERVICE_AVAILABLE', True),
                      mock.patch.object(iscsi_views, '_status', return_value={'service': {}})):
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def status(self, *items):
        data = {'bindings': [dict({'dev': '/dev/sg6', 'current': '/dev/sg3',
                                   'unit': 'vtllibrary@70.service', 'reason': 'why',
                                   'address': [0, 0, 0], 'luns': []}, **item)
                             for item in items], 'stale': []}
        return mock.patch.object(IscsiService, 'binding_status',
                                 return_value=success_result('checked', data))

    def test_a_stale_export_is_flagged_with_a_rebind_button(self):
        with self.status({'name': 'lib70_changer', 'library_id': 70, 'status': 'stale'}):
            page = self.client.get(reverse('libraries:iscsi_dashboard'))
        self.assertContains(page, 'no longer')
        self.assertContains(page, 'Re-bind library 70')

    def test_bound_exports_offer_no_rebind(self):
        with self.status({'name': 'lib70_changer', 'library_id': 70, 'status': 'bound'}):
            page = self.client.get(reverse('libraries:iscsi_dashboard'))
        self.assertContains(page, 'lib70_changer')
        self.assertNotContains(page, 'Re-bind library')

    def test_rebind_posts_to_the_service_for_that_library(self):
        with mock.patch.object(IscsiService, 'rebind_library',
                               return_value=success_result('Library 70: rebound 1')) as rebind:
            response = self.client.post(reverse('libraries:iscsi_rebind'), {'library_id': '70'})
        rebind.assert_called_once_with(70)
        self.assertRedirects(response, reverse('libraries:iscsi_dashboard'),
                             fetch_redirect_response=False)


class BootRecordTests(SimpleTestCase):
    """remap records the saved backstores just before target.service binds them,
    or every export would read as stale after a reboot."""

    def test_saved_backstores_are_recorded_against_the_daemon_at_their_node(self):
        saved = {'storage_objects': [
            {'plugin': 'pscsi', 'name': 'lib70_changer', 'dev': '/dev/sg4'},
            {'plugin': 'pscsi', 'name': 'STK_t10000b_2', 'dev': '/dev/sg7'},
            {'plugin': 'pscsi', 'name': 'gone', 'dev': '/dev/sg99'},
            {'plugin': 'block', 'name': 'disk', 'dev': '/dev/sdb'}]}
        devices = [device((0, 0, 0), '/dev/sg4'), device((0, 2, 0), '/dev/sg7')]
        with mock.patch.object(bindings, 'invocation_ids', return_value=INVOCATIONS):
            state = bindings.record_saved(saved, Conf(), devices)
        self.assertEqual(sorted(state), ['STK_t10000b_2', 'lib70_changer'])
        self.assertEqual(state['lib70_changer']['unit'], 'vtllibrary@70.service')
        self.assertEqual(state['lib70_changer']['invocation'], 'aaa')
        self.assertEqual(state['STK_t10000b_2']['unit'], 'vtltape@72.service')

    def test_a_boot_record_reads_as_bound(self):
        saved = {'storage_objects': [{'plugin': 'pscsi', 'name': 'lib70_changer',
                                      'dev': '/dev/sg4'}]}
        devices = [device((0, 0, 0), '/dev/sg4')]
        with mock.patch.object(bindings, 'invocation_ids', return_value=INVOCATIONS):
            state = bindings.record_saved(saved, Conf(), devices)
        found = assess(config(('lib70_changer', '/dev/sg4')), devices, state)
        self.assertEqual(found['lib70_changer'].status, bindings.BOUND)

    def test_remap_does_not_record_under_the_test_settings(self):
        from apps.libraries.services.iscsi import remap
        with mock.patch.object(bindings, 'save_state') as save:
            remap._record_for_restore({}, Conf(), [])
        save.assert_not_called()


@override_settings(MHVTL_ISCSI_REBIND=True)
class ReleaseTests(SimpleTestCase):
    """A library's exports are released before it is removed: a device removed
    while a backstore holds it is never freed, and poisons its address."""

    def setUp(self):
        cfg = config(('lib70_changer', '/dev/sg4'))
        cfg['storage_objects'].append({'plugin': 'pscsi', 'name': 'other', 'dev': '/dev/sg1'})
        found = list(assess(cfg, [device((0, 0, 0), '/dev/sg4'), device((0, 9, 0), '/dev/sg1')],
                            addresses={'lib70_changer': (0, 0, 0), 'other': (0, 9, 0)}).values())
        self.state = {'lib70_changer': {'unit': 'vtllibrary@70.service'}}
        patches = [mock.patch.object(bindings, '_gather',
                                     return_value=((cfg, Conf(), found, self.state), '')),
                   mock.patch.object(bindings.targetcli, 'save_config'),
                   mock.patch.object(bindings, 'save_state')]
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def test_only_that_librarys_backstores_are_deleted(self):
        with mock.patch.object(bindings.targetcli, 'delete_backstore',
                               return_value=CommandResult([], 0, '', '')) as delete:
            result = bindings.release_library(70)
        delete.assert_called_once_with('pscsi', 'lib70_changer')
        self.assertEqual(result.data['released'], ['lib70_changer'])
        self.assertNotIn('lib70_changer', self.state)

    def test_a_failed_release_is_a_failure(self):
        with mock.patch.object(bindings.targetcli, 'delete_backstore',
                               return_value=CommandResult([], 1, '', 'busy')):
            self.assertFalse(bindings.release_library(70).success)


class RemovalRefusedTests(SimpleTestCase):

    def test_a_library_is_not_removed_while_its_export_cannot_be_released(self):
        from apps.libraries.services.core import failure_result
        from apps.libraries.services.libraries import service as library_service
        with mock.patch.object(library_service.iscsi_bindings, 'release_library',
                               return_value=failure_result('Could not release', ['busy'])), \
             mock.patch.object(library_service.lifecycle, 'delete') as delete:
            result = library_service.LibraryService('/tmp/unused').delete(70)
        self.assertFalse(result.success)
        delete.assert_not_called()


class ExportNamingTests(SimpleTestCase):
    """Backstores are named lib<L>_changer / lib<L>_drive<N>, which remap reads."""

    def test_the_changer_and_drives_get_the_names_remap_understands(self):
        from apps.libraries.services.iscsi import workflow
        service = mock.Mock()
        service.create_target.return_value = success_result('ok')
        service.create_backstore.return_value = success_result('ok')
        service.create_lun.return_value = success_result('ok')
        service.set_generate_node_acls.return_value = success_result('ok')
        workflow.export_library(40, [{'device_path': '/dev/sg20', 'type': 'tape'},
                                     {'device_path': '/dev/sg18', 'type': 'changer'},
                                     {'device_path': '/dev/sg21', 'type': 'tape'}],
                                iqn=IQN, service=service)
        names = [c.args[1] for c in service.create_backstore.call_args_list]
        self.assertEqual(names, ['lib40_changer', 'lib40_drive0', 'lib40_drive1'])


class AttachTests(SimpleTestCase):
    """Attach records one target, manual-start: discovery once recorded every
    target the portal offered as automatic, and after a reboot this host was
    logged in to library 10's export."""

    def test_attach_never_discovers_and_never_starts_at_boot(self):
        from apps.libraries.services.iscsi import initiator
        ran = []
        with mock.patch.object(initiator.shell, 'sudo',
                               side_effect=lambda argv, **kw: ran.append(argv) or
                               CommandResult(argv, 0, '', '')), \
             mock.patch.object(initiator, 'session_for', return_value=None), \
             mock.patch.object(initiator, 'portal_for', return_value='127.0.0.1'), \
             mock.patch.object(initiator, 'exported_as', return_value={}), \
             mock.patch.object(initiator, 'attached_devices', return_value=[]):
            self.assertTrue(initiator.attach(IQN, wait=0).success)
        flat = [' '.join(argv) for argv in ran]
        self.assertFalse(any('discovery' in line for line in flat), flat)
        self.assertIn(f'iscsiadm -m node -T {IQN} -p 127.0.0.1 -o new', flat)
        self.assertIn(f'iscsiadm -m node -T {IQN} -p 127.0.0.1 -o update '
                      f'-n node.startup -v manual', flat)
        self.assertEqual(flat[-1], f'iscsiadm -m node -T {IQN} -p 127.0.0.1 --login')


class PortalTests(SimpleTestCase):
    """This host reaches its own target where the target listens."""

    def portal(self, *portals):
        from apps.libraries.services.iscsi import initiator
        cfg = {'targets': [{'wwn': IQN, 'tpgs': [{'portals': [
            {'ip_address': ip, 'port': port} for ip, port in portals]}]}]}
        return initiator.portal_for(IQN, cfg)

    def test_all_addresses_is_reached_on_loopback_at_its_port(self):
        self.assertEqual(self.portal(('0.0.0.0', 3260)), '127.0.0.1:3260')

    def test_a_loopback_portal_on_its_own_port_is_used_as_it_is(self):
        """127.0.0.1:3260 cannot be bound while library 10 listens on 0.0.0.0:3260."""
        self.assertEqual(self.portal(('127.0.0.1', 3261)), '127.0.0.1:3261')

    def test_no_portal_falls_back_to_the_default(self):
        self.assertEqual(self.portal(), '127.0.0.1:3260')



@override_settings(MHVTL_ISCSI_REBIND=True)
class RestoreWithheldTests(SimpleTestCase):

    def setUp(self):
        self.entry = {'address': [0, 0, 0], 'dev': '/dev/sg6',
                      'luns': [{'fabric': 'iscsi', 'wwn': IQN, 'tpg': 1, 'index': 0,
                                'mapped': [{'node_wwn': 'iqn.2026-09.com.example:host',
                                            'index': 5, 'write_protect': False}]}]}
        self.commands = []
        patches = [
            mock.patch.object(bindings, 'load_withheld',
                              return_value={'lib70_changer': dict(self.entry)}),
            mock.patch.object(bindings, 'save_withheld'),
            mock.patch.object(bindings, 'record'),
            mock.patch.object(bindings.targetcli, 'save_config'),
            mock.patch.object(bindings.targetcli, 'validate_device'),
            mock.patch.object(bindings.targetcli, 'run', side_effect=lambda a, **k:
                              self.commands.append(list(a)) or CommandResult(a, 0, '', '')),
            mock.patch('apps.libraries.services.config.service.ConfigService.device_conf',
                       return_value=Conf()),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def test_it_is_created_on_the_device_now_at_its_address(self):
        """Never on the node it had before the reboot."""
        with mock.patch.object(bindings.lsscsi, 'local',
                               return_value=[device((0, 0, 0), '/dev/sg3')]):
            done = bindings.restore_withheld(70)
        self.assertEqual(done[0]['restored'], True)
        self.assertEqual(self.commands, [
            ['/backstores/pscsi', 'create', 'name=lib70_changer', 'dev=/dev/sg3'],
            [f'/iscsi/{IQN}/tpg1/luns', 'create', '/backstores/pscsi/lib70_changer',
             'lun=0', 'add_mapped_luns=false'],
            [f'/iscsi/{IQN}/tpg1/acls/iqn.2026-09.com.example:host', 'create',
             'mapped_lun=5', 'tpg_lun_or_backstore=0', 'write_protect=0']])
        bindings.save_withheld.assert_called_once_with({})

    def test_it_stays_withheld_while_its_device_is_missing(self):
        with mock.patch.object(bindings.lsscsi, 'local', return_value=[]):
            done = bindings.restore_withheld(70)
        self.assertEqual(done[0]['restored'], False)
        self.assertEqual(self.commands, [])
        bindings.save_withheld.assert_called_once_with({'lib70_changer': self.entry})


class RecordBindingViewTests(SimpleTestCase):
    """Recording a binding for a backstore the GUI did not create.

    targetcli's backstores work, but the GUI never saw which daemon start they
    bound to, so they read *unknown* and the boot check cannot judge them.
    Recording says they are right as they stand; it changes nothing in the
    target.
    """

    def _post(self, data):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import iscsi_views
        request = RequestFactory().post('/libraries/iscsi/record-binding/', data)
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        response = iscsi_views.IscsiRecordBindingView().post(request)
        return response, [str(m) for m in request._messages]

    def test_it_records_the_named_backstore_only(self):
        from apps.libraries import iscsi_views
        from apps.libraries.services.core import success_result
        with mock.patch.object(iscsi_views.bindings, 'record',
                               return_value=success_result('1 binding recorded')) as record:
            response, notes = self._post({'name': 'lib40_changer'})
        record.assert_called_once_with(['lib40_changer'])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('recorded' in note for note in notes), notes)

    def test_no_name_records_every_binding_that_is_right(self):
        from apps.libraries import iscsi_views
        from apps.libraries.services.core import success_result
        with mock.patch.object(iscsi_views.bindings, 'record',
                               return_value=success_result('3 recorded')) as record:
            self._post({})
        record.assert_called_once_with(None)

    def test_a_refusal_is_reported(self):
        from apps.libraries import iscsi_views
        from apps.libraries.services.core import failure_result
        with mock.patch.object(iscsi_views.bindings, 'record',
                               return_value=failure_result('rebinding is off here',
                                                           ['MHVTL_ISCSI_REBIND'])):
            _, notes = self._post({'name': 'lib40_changer'})
        self.assertTrue(any('rebinding is off here' in note for note in notes), notes)

    def test_it_needs_a_login(self):
        from django.test import RequestFactory
        from apps.libraries import iscsi_views
        request = RequestFactory().post('/libraries/iscsi/record-binding/')
        request.session = {}
        with mock.patch.object(iscsi_views.bindings, 'record') as record:
            response = iscsi_views.IscsiRecordBindingView().post(request)
        record.assert_not_called()
        self.assertEqual(response.status_code, 302)
