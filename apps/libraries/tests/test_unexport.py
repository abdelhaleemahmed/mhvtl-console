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
        """The target step names the target, because there can be more than
        one: a library exported twice under different names has two, and a
        report that said "delete target" once would hide the second."""
        service = self._service()
        steps = [s['step'] for s in self._unexport(service).data['steps']]
        self.assertEqual(steps,
                         ['delete target iqn.2026-09.com.mhvtl:library50',
                          'remove backstore lib50_changer',
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


class FoundByItsBackstoresTests(SimpleTestCase):
    """Unexport finds the target by what it exports, not by its name.

    It used to generate the name with default_iqn(), which stamps today's
    year and month. A library exported in one month and unexported in the next
    was looked for under a name that had never existed - not found, reported
    as success, and its backstores deleted anyway. That leaves a live target
    whose LUNs point at nothing.
    """

    def _unexport(self, service, library_id=50, **kwargs):
        with mock.patch.object(workflow.bindings, 'forget', return_value=True):
            return workflow.unexport_library(library_id, service=service,
                                             **kwargs)

    @staticmethod
    def _service(*targets, **kwargs):
        return FakeService(
            targets=list(targets),
            backstores=[{'name': 'lib50_changer', 'plugin': 'pscsi'},
                        {'name': 'lib50_drive0', 'plugin': 'pscsi'}],
            **kwargs)

    def test_a_target_named_in_another_month_is_still_found(self):
        """The bug, in one test: exported under September's name, unexported
        in whatever month this suite runs in."""
        service = self._service(target('iqn.2024-01.com.mhvtl:library50',
                                       'lib50_changer', 'lib50_drive0'))
        result = self._unexport(service)
        self.assertTrue(result.success, result.message)
        self.assertIn('delete_target:iqn.2024-01.com.mhvtl:library50',
                      service.calls)

    def test_a_target_named_by_hand_is_found_too(self):
        """An export made with --iqn says nothing about the library either;
        the date was only the commonest way to hit that."""
        service = self._service(target('iqn.example:whatever-i-like',
                                       'lib50_changer', 'lib50_drive0'))
        self._unexport(service)
        self.assertIn('delete_target:iqn.example:whatever-i-like',
                      service.calls)

    def test_another_library_s_target_is_left_alone(self):
        service = self._service(target('iqn.2024-01.com.mhvtl:library10',
                                       'lib10_changer'))
        self._unexport(service)
        self.assertEqual([c for c in service.calls
                          if c.startswith('delete_target')], [])

    def test_a_target_that_is_not_ours_is_left_alone(self):
        service = self._service(target('iqn.2024-01.com.example:backups',
                                       'somebody_elses'))
        self._unexport(service)
        self.assertEqual([c for c in service.calls
                          if c.startswith('delete_target')], [])

    def test_a_library_exported_twice_loses_both_targets(self):
        """Two targets for one library is the same bug seen from the other
        side - exporting in a second month made a second name."""
        service = self._service(
            target('iqn.2024-01.com.mhvtl:library50', 'lib50_changer'),
            target('iqn.2024-05.com.mhvtl:library50', 'lib50_drive0'))
        result = self._unexport(service)
        self.assertTrue(result.success, result.message)
        self.assertEqual(
            sorted(c for c in service.calls if c.startswith('delete_target')),
            ['delete_target:iqn.2024-01.com.mhvtl:library50',
             'delete_target:iqn.2024-05.com.mhvtl:library50'])

    def test_a_named_iqn_is_still_obeyed(self):
        """A caller that names one means it, and the web always does."""
        service = self._service(
            target('iqn.2024-01.com.mhvtl:library50', 'lib50_changer'),
            target('iqn.2024-05.com.mhvtl:library50', 'lib50_drive0'))
        self._unexport(service, iqn='iqn.2024-05.com.mhvtl:library50')
        self.assertEqual(
            [c for c in service.calls if c.startswith('delete_target')],
            ['delete_target:iqn.2024-05.com.mhvtl:library50'])

    def test_no_target_at_all_is_not_a_failure(self):
        """The backstores it left are exactly what this call is for."""
        service = self._service()
        result = self._unexport(service)
        self.assertTrue(result.success, result.message)
        self.assertIn('delete_backstore:lib50_changer', service.calls)


class AMissReadsDifferentlyFromAnAbsenceTests(SimpleTestCase):
    """"Already gone" and "we did not find it" used to report the same.

    With the lookup fixed a genuine miss is nearly impossible, but the two
    situations are still different and the report should say which it met.
    """

    def _unexport(self, service, library_id=50, **kwargs):
        with mock.patch.object(workflow.bindings, 'forget', return_value=True):
            return workflow.unexport_library(library_id, service=service,
                                             **kwargs)

    @staticmethod
    def _service(targets=(), backstores=()):
        return FakeService(targets=list(targets), backstores=list(backstores))

    def test_nothing_at_all_is_a_clean_no_op(self):
        """Unexport run twice, or a library never exported."""
        result = self._unexport(self._service())
        self.assertTrue(result.success)
        self.assertEqual(result.data['warnings'], [])
        step = result.data['steps'][0]
        self.assertTrue(step['ok'])
        self.assertIn('nothing exports library 50', step['detail'])

    def test_backstores_with_no_target_is_said_out_loud(self):
        """A half-finished export, a target deleted by hand, or an earlier
        unexport that looked under a generated name and missed - which is the
        damage this plan exists for."""
        service = self._service(backstores=[
            {'name': 'lib50_changer', 'plugin': 'pscsi'},
            {'name': 'lib50_drive0', 'plugin': 'pscsi'}])
        result = self._unexport(service)
        step = result.data['steps'][0]
        self.assertFalse(step['ok'])
        self.assertIn('no target exports library 50', step['detail'])
        self.assertIn('lib50_changer', step['detail'])
        self.assertEqual(len(result.data['warnings']), 1)

    def test_it_still_removes_them_and_still_succeeds(self):
        """Removing them is what the call is for; saying so is the addition."""
        service = self._service(backstores=[
            {'name': 'lib50_changer', 'plugin': 'pscsi'}])
        result = self._unexport(service)
        self.assertTrue(result.success, result.message)
        self.assertIn('delete_backstore:lib50_changer', service.calls)
        self.assertIn('warning', result.message)

    def test_another_library_s_backstores_do_not_raise_it(self):
        service = self._service(backstores=[
            {'name': 'lib10_changer', 'plugin': 'pscsi'},
            {'name': 'somebody_elses', 'plugin': 'fileio'}])
        result = self._unexport(service)
        self.assertTrue(result.data['steps'][0]['ok'])
        self.assertEqual(result.data['warnings'], [])

    def test_keeping_the_backstores_still_looks_before_it_speaks(self):
        """remove_backstores=False does not read them, so it cannot claim
        they are not there."""
        service = self._service(backstores=[
            {'name': 'lib50_changer', 'plugin': 'pscsi'}])
        result = self._unexport(service, remove_backstores=False)
        self.assertTrue(result.success)
        self.assertEqual([c for c in service.calls
                          if c.startswith('delete_backstore')], [])
