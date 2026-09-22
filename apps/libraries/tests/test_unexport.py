"""Unexporting a library: the target, and the devices it exported.

Deleting a target alone leaves its pscsi backstores behind, each holding the
/dev/sg node it was made with. That is right for a backstore on its own - the
same device can be mapped into another target - and wrong for a library that
is no longer exported: the next library at that SCSI address wants the node,
and `iscsi remap` goes on trying to repoint a backstore at a daemon that is
gone.

Nothing here touches targetcli: the service is faked, and what is checked is
which calls were made and in which order.
"""
import json
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.libraries.services.core import failure_result, success_result
from apps.libraries.services.iscsi import workflow


def target(iqn, *backstores):
    return {'iqn': iqn,
            'tpgs': [{'luns': [{'lun_id': i, 'backstore_name': name}
                               for i, name in enumerate(backstores)]}]}


class FakeService:
    """Records what the workflow asked for, and answers success."""

    def __init__(self, targets=(), backstores=(), fails=()):
        self._targets = list(targets)
        self._backstores = list(backstores)
        self.fails = set(fails)
        self.calls = []

    def _answer(self, what, message):
        self.calls.append(what)
        if what in self.fails:
            return failure_result(f'{what} failed', [f'{what} failed'], 'op')
        return success_result(message, {}, 'op')

    def targets(self):
        return success_result('targets', {'targets': self._targets}, 'op')

    def backstores(self):
        return success_result('backstores', {'backstores': self._backstores}, 'op')

    def delete_target(self, iqn):
        return self._answer(f'delete_target:{iqn}', f'Deleted {iqn}')

    def delete_backstore(self, plugin, name):
        return self._answer(f'delete_backstore:{name}', f'Deleted {name}')

    def save_config(self):
        return self._answer('save_config', 'Saved')


class LibraryOfTests(SimpleTestCase):
    """Which library a target exports, read from its LUNs."""

    def test_it_reads_the_backstore_names(self):
        self.assertEqual(
            workflow.library_of(target('iqn.x:any', 'lib50_changer',
                                       'lib50_drive0', 'lib50_drive1')), 50)

    def test_a_target_of_another_kind_is_not_ours(self):
        """A backstore made by hand has a name this did not write."""
        self.assertIsNone(workflow.library_of(target('iqn.x:any', 'my_own_disk')))

    def test_a_target_spanning_two_libraries_is_not_one_library(self):
        self.assertIsNone(
            workflow.library_of(target('iqn.x:any', 'lib10_changer', 'lib20_drive0')))

    def test_the_name_of_the_target_is_not_consulted(self):
        """An export made with --iqn says nothing about the library; the
        backstores are what this code wrote."""
        self.assertEqual(
            workflow.library_of(target('iqn.2026-09.com.example:backups',
                                       'lib30_changer')), 30)

    def test_a_target_with_no_luns(self):
        self.assertIsNone(workflow.library_of({'iqn': 'iqn.x:any', 'tpgs': [{}]}))


class UnexportTests(SimpleTestCase):

    def _service(self, **kwargs):
        return FakeService(
            targets=[target('iqn.2026-09.com.mhvtl:library50', 'lib50_changer',
                            'lib50_drive0', 'lib50_drive1')],
            backstores=[{'name': 'lib50_changer', 'plugin': 'pscsi'},
                        {'name': 'lib50_drive0', 'plugin': 'pscsi'},
                        {'name': 'lib50_drive1', 'plugin': 'pscsi'},
                        {'name': 'lib10_changer', 'plugin': 'pscsi'},
                        {'name': 'somebody_elses', 'plugin': 'fileio'}],
            **kwargs)

    def _unexport(self, service, library_id=50, **kwargs):
        with mock.patch.object(workflow.bindings, 'forget', return_value=True):
            return workflow.unexport_library(library_id, service=service, **kwargs)

    def test_it_deletes_the_target_and_this_library_s_backstores(self):
        service = self._service()
        result = self._unexport(service)
        self.assertTrue(result.success, result.message)
        self.assertEqual(service.calls, [
            'delete_target:iqn.2026-09.com.mhvtl:library50',
            'delete_backstore:lib50_changer',
            'delete_backstore:lib50_drive0',
            'delete_backstore:lib50_drive1',
            'save_config'])

    def test_another_library_s_backstores_are_left_alone(self):
        service = self._service()
        self._unexport(service)
        self.assertNotIn('delete_backstore:lib10_changer', service.calls)

    def test_a_name_that_only_looks_like_ours_is_left_alone(self):
        """The rule was written twice - a regex in one reader, a string split
        in the other - and the split accepted lib50_drivefoo. One expression
        now, used by both."""
        service = FakeService(
            targets=[],
            backstores=[{'name': 'lib50_drive0', 'plugin': 'pscsi'},
                        {'name': 'lib50_drivefoo', 'plugin': 'pscsi'},
                        {'name': 'lib50_whatever', 'plugin': 'pscsi'},
                        {'name': 'lib5_drive0', 'plugin': 'pscsi'}])
        self._unexport(service)
        removed = [c for c in service.calls if c.startswith('delete_backstore')]
        self.assertEqual(removed, ['delete_backstore:lib50_drive0'])

    def test_a_backstore_made_by_hand_is_left_alone(self):
        """Its name is not one this code writes, so removing it is not ours
        to decide."""
        service = self._service()
        self._unexport(service)
        self.assertNotIn('delete_backstore:somebody_elses', service.calls)

    def test_the_record_of_the_bindings_goes_too(self):
        """A binding kept for a backstore that no longer exists makes remap
        chase something that is not there."""
        service = self._service()
        with mock.patch.object(workflow.bindings, 'forget',
                               return_value=True) as forget:
            workflow.unexport_library(50, service=service)
        forget.assert_called_once()
        self.assertEqual(sorted(forget.call_args.args[0]),
                         ['lib50_changer', 'lib50_drive0', 'lib50_drive1'])

    def test_a_target_that_is_already_gone_is_not_an_error(self):
        """The backstores it left behind are exactly what this is for."""
        service = FakeService(targets=[], backstores=[
            {'name': 'lib50_changer', 'plugin': 'pscsi'}])
        result = self._unexport(service)
        self.assertTrue(result.success, result.message)
        self.assertIn('delete_backstore:lib50_changer', service.calls)

    def test_a_target_that_will_not_delete_stops_it(self):
        """Removing the devices out from under a target that still exports
        them would be worse than stopping."""
        service = self._service(
            fails=['delete_target:iqn.2026-09.com.mhvtl:library50'])
        result = self._unexport(service)
        self.assertFalse(result.success)
        self.assertNotIn('delete_backstore:lib50_changer', service.calls)

    def test_keeping_the_backstores_is_possible_for_a_caller_that_means_it(self):
        service = self._service()
        result = self._unexport(service, remove_backstores=False)
        self.assertTrue(result.success)
        self.assertEqual(service.calls,
                         ['delete_target:iqn.2026-09.com.mhvtl:library50',
                          'save_config'])

    def test_it_reports_every_step(self):
        service = self._service()
        steps = [s['step'] for s in self._unexport(service).data['steps']]
        self.assertEqual(steps, ['delete target', 'remove backstore lib50_changer',
                                 'remove backstore lib50_drive0',
                                 'remove backstore lib50_drive1',
                                 'forget the bindings',
                                 'save the configuration'])


class DeleteTargetEndpointTests(TestCase):
    """The page's dialog: delete the target, or unexport the library."""

    def _post(self, payload):
        from apps.libraries import iscsi_views

        request = RequestFactory().post(
            '/libraries/ajax/iscsi/target/delete/',
            data=json.dumps(payload), content_type='application/json')
        request.session = {'mhvtl_logged_in': True}
        return iscsi_views.DeleteTargetAjaxView.as_view()(request)

    def test_without_the_checkbox_it_only_deletes_the_target(self):
        from apps.libraries import iscsi_views

        with mock.patch.object(iscsi_views.IscsiService, 'delete_target',
                               return_value=success_result('gone', {}, 'op')) as delete, \
             mock.patch.object(iscsi_views.IscsiService, 'unexport_library') as unexport:
            self._post({'iqn': 'iqn.x:library50', 'library_id': 50})
        delete.assert_called_once()
        unexport.assert_not_called()

    def test_with_the_checkbox_it_unexports_the_library(self):
        from apps.libraries import iscsi_views

        with mock.patch.object(iscsi_views.IscsiService, 'unexport_library',
                               return_value=success_result('gone', {}, 'op')) as unexport, \
             mock.patch.object(iscsi_views.IscsiService, 'delete_target') as delete:
            self._post({'iqn': 'iqn.x:library50', 'library_id': 50,
                        'remove_backstores': True})
        unexport.assert_called_once_with(50, iqn='iqn.x:library50')
        delete.assert_not_called()

    def test_a_target_that_is_no_library_cannot_unexport(self):
        """The checkbox is not offered without a library, and the endpoint
        does not take its word for it either."""
        from apps.libraries import iscsi_views

        with mock.patch.object(iscsi_views.IscsiService, 'delete_target',
                               return_value=success_result('gone', {}, 'op')) as delete, \
             mock.patch.object(iscsi_views.IscsiService, 'unexport_library') as unexport:
            self._post({'iqn': 'iqn.x:any', 'remove_backstores': True})
        delete.assert_called_once()
        unexport.assert_not_called()
