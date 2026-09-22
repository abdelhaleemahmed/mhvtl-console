"""The pages the manual export path uses: Create Target's portal box, and
restarting from the console's Service Status page with exports rebound."""
from unittest import mock

from django.urls import reverse

from apps.libraries import console_views
from apps.libraries.services.core import success_result
from apps.libraries.services.iscsi.service import IscsiService
from apps.libraries.services.libraries import LibraryService
from apps.libraries.tests.base import LibraryTestBase

IQN = 'iqn.2026-09.com.example:tapevault'


class PageTestBase(LibraryTestBase):

    def setUp(self):
        super().setUp()
        self.login_as_user()
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()


class CreateTargetPortalTests(PageTestBase):
    """targetcli adds 0.0.0.0:3260 to every new target by itself."""

    def post(self, **extra):
        with mock.patch.object(IscsiService, 'create_target',
                               return_value=success_result('ok')), \
             mock.patch.object(IscsiService, 'delete_portal',
                               return_value=success_result('ok')) as delete, \
             mock.patch.object(IscsiService, 'create_portal') as create:
            self.client.post(reverse('libraries:iscsi_create_target'), {'iqn': IQN, **extra})
        return delete, create

    def test_unticked_removes_the_portal_targetcli_added(self):
        delete, create = self.post()
        delete.assert_called_once_with(IQN, '0.0.0.0', 3260)
        create.assert_not_called()

    def test_ticked_keeps_it_and_does_not_add_a_second(self):
        delete, create = self.post(add_default_portal='on')
        delete.assert_not_called()
        create.assert_not_called()


class ServiceStatusRestartTests(PageTestBase):

    def test_restarting_one_library_goes_through_restart_services(self):
        with mock.patch.object(LibraryService, 'restart_services',
                               return_value=success_result(
                                   'Restarted vtllibrary@50.service; Library 50: rebound 3 '
                                   'exported device(s)')) as restart:
            self.client.post(reverse('libraries:console_service_status'),
                             {'action': 'restart_library', 'library_id': '50'})
        restart.assert_called_once_with(50)

    def test_restart_all_rebinds_every_export(self):
        from apps.libraries.services.iscsi import bindings
        with mock.patch.object(console_views, '_control', return_value={'success': True}), \
             mock.patch.object(bindings, 'after_restart', return_value='') as rebind:
            self.client.post(reverse('libraries:console_service_status'), {'action': 'restart'})
        rebind.assert_called_once_with(None)

    def test_stop_rebinds_nothing(self):
        from apps.libraries.services.iscsi import bindings
        with mock.patch.object(console_views, '_control', return_value={'success': True}), \
             mock.patch.object(bindings, 'after_restart') as rebind:
            self.client.post(reverse('libraries:console_service_status'), {'action': 'stop'})
        rebind.assert_not_called()


class LunNumberTests(PageTestBase):

    def test_targetcli_gets_the_lun_number_when_one_is_given(self):
        from apps.libraries.services.iscsi import targetcli
        with mock.patch.object(targetcli, 'run') as run:
            targetcli.create_lun(IQN, 'pscsi', 'lib50_changer', lun=0)
            targetcli.create_lun(IQN, 'pscsi', 'lib50_drive0')
        self.assertEqual(run.call_args_list[0].args[0][-1], 'lun=0')
        self.assertEqual(run.call_args_list[1].args[0][-1], '/backstores/pscsi/lib50_drive0')


class BackstoreNamingTests(PageTestBase):
    """The backstores page suggests the names remap.py repoints after a reboot."""

    def test_each_device_says_its_library_and_the_name_remap_understands(self):
        from apps.libraries.services.scsi import lsscsi
        conf = mock.Mock(libraries={50: {}},
                         address_of=lambda i: {50: (0, 18, 0), 51: (0, 19, 0), 52: (0, 20, 0)}.get(i),
                         drives_of=lambda lib: {51: {}, 52: {}} if lib == 50 else {})
        devices = lsscsi.parse(
            '[16:0:18:0]  mediumx IBM      3573-TL          D.02  -          /dev/sg18\n'
            '[16:0:20:0]  tape    IBM      ULT3580-HHA      D.02  /dev/st13  /dev/sg20\n')
        with mock.patch.object(lsscsi, 'local', return_value=devices), \
             mock.patch('apps.libraries.services.config.service.ConfigService.device_conf',
                        return_value=conf):
            found = IscsiService().available_devices().data['devices']
        self.assertEqual([(d['belongs_to'], d['suggested_name']) for d in found],
                         [('library 50 changer', 'lib50_changer'),
                          ('library 50 drive 1', 'lib50_drive1')])


class LocalInitiatorTests(PageTestBase):

    def test_the_initiator_name_is_read_from_its_file(self):
        import tempfile
        from pathlib import Path
        from apps.libraries.services.iscsi import initiator
        with tempfile.NamedTemporaryFile('w', suffix='.iscsi', delete=False) as f:
            f.write('InitiatorName=iqn.1994-05.com.redhat:abc123\n')
        with mock.patch.object(initiator, 'INITIATOR_NAME', Path(f.name)):
            self.assertEqual(initiator.local_name(), 'iqn.1994-05.com.redhat:abc123')
        Path(f.name).unlink()


class ServiceStatusPageTests(PageTestBase):

    def test_each_library_unit_shows_its_id_and_a_restart_button(self):
        from apps.libraries.services.console import units
        state = units.MhvtlServiceStatus(
            target_active=True,
            libraries=[units.UnitState('vtllibrary@50.service', True, 'running')],
            drives=[units.UnitState('vtltape@51.service', True, 'running')])
        with mock.patch.object(units, 'status', return_value=state), \
             mock.patch.object(console_views.modules, 'summary', return_value={}):
            page = self.client.get(reverse('libraries:console_service_status'))
        self.assertContains(page, 'ID: 50')
        self.assertContains(page, 'id="restart-library-50"')
        self.assertContains(page, 'ID: 51')
