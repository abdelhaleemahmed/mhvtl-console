"""The LTFS endpoints: the web console calling the same service as the CLI.

These check the contract the page depends on - authentication, the method, the
shape of the reply - and that each endpoint reaches the service method it says
it does. What the service decides is tested in test_ltfs_mounts.py; nothing is
decided here.
"""
import json
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.libraries.services.core.results import success_result


def signed_in(client):
    """Both layers of this console's authentication.

    LoginRequiredMiddleware wants request.user, and the views additionally check
    an mhvtl_logged_in session key. A test that sets only one gets a 403 from
    the layer it forgot.
    """
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.create_user(
        username='ltfs-test', password='not-used')
    client.force_login(user)
    session = client.session
    session['mhvtl_logged_in'] = True
    session.save()
    return client


class LtfsEndpointTests(TestCase):

    def setUp(self):
        signed_in(self.client)

    # -- reading ------------------------------------------------------------

    def test_drive_status_returns_what_the_service_said(self):
        payload = success_result('two drives', {'drives': [], 'tools': {},
                                                'format_allowed': False})
        with mock.patch('apps.libraries.tape_operations_views._ltfs') as factory:
            factory.return_value.status.return_value = payload
            response = self.client.get(
                reverse('libraries:api_ltfs_drives', args=[60]))
            factory.return_value.status.assert_called_once_with(60)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['message'], 'two drives')

    def test_drive_status_needs_a_session(self):
        """Refused without one. Which code depends on middleware ordering
        rather than on this view - an unauthenticated GET under /libraries/api/
        is redirected, because LoginRequiredMiddleware's JSON detection matches
        a path starting with /api/ and these endpoints are mounted deeper. That
        is true of every api/ endpoint in this app, not only this one, so the
        contract asserted here is 'not served', not a number."""
        self.client.logout()
        with mock.patch('apps.libraries.tape_operations_views._ltfs') as factory:
            response = self.client.get(
                reverse('libraries:api_ltfs_drives', args=[60]))
            factory.return_value.status.assert_not_called()
        self.assertNotEqual(response.status_code, 200)

    # -- the three actions --------------------------------------------------

    ACTIONS = (('ltfs_mount_ajax', 'mount'),
               ('ltfs_unmount_ajax', 'unmount'),
               ('ltfs_check_ajax', 'check'))

    def test_each_action_calls_its_own_service_method(self):
        for url_name, verb in self.ACTIONS:
            with self.subTest(verb=verb):
                with mock.patch(
                        'apps.libraries.tape_operations_views._ltfs') as factory:
                    getattr(factory.return_value, verb).return_value = \
                        success_result(f'{verb} done', {})
                    response = self.client.post(
                        reverse(f'libraries:{url_name}'),
                        data=json.dumps({'library_id': 60, 'drive': 0}),
                        content_type='application/json')
                    getattr(factory.return_value, verb).assert_called_once_with(60, 0)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])

    def test_a_get_on_an_action_is_refused_with_json(self):
        """It is an endpoint, not a page, so it says so in the JSON the caller
        is already parsing rather than returning a blank 405 body."""
        for url_name, _ in self.ACTIONS:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(f'libraries:{url_name}'))
                self.assertEqual(response.status_code, 405)
                self.assertFalse(response.json()['success'])

    def test_an_action_needs_a_session(self):
        """Nothing is mounted or unmounted without one. CSRF refuses the POST
        before the session is even considered, which is the right order and is
        why the assertion is on the service, not on the status code."""
        self.client.logout()
        for url_name, verb in self.ACTIONS:
            with self.subTest(url=url_name):
                with mock.patch(
                        'apps.libraries.tape_operations_views._ltfs') as factory:
                    response = self.client.post(
                        reverse(f'libraries:{url_name}'),
                        data=json.dumps({'library_id': 60, 'drive': 0}),
                        content_type='application/json')
                    getattr(factory.return_value, verb).assert_not_called()
                self.assertNotEqual(response.status_code, 200)

    def test_a_missing_drive_is_a_400_and_not_a_traceback(self):
        for body in ('{}', '{"library_id": 60}', 'not json', ''):
            with self.subTest(body=body):
                with mock.patch(
                        'apps.libraries.tape_operations_views._ltfs') as factory:
                    response = self.client.post(
                        reverse('libraries:ltfs_mount_ajax'), data=body,
                        content_type='application/json')
                    factory.return_value.mount.assert_not_called()
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()['success'])

    def test_a_service_refusal_is_passed_through_as_a_200_with_success_false(self):
        """The page distinguishes 'could not' from 'broke' by the body, which is
        how every other endpoint here behaves."""
        from apps.libraries.services.core.results import failure_result
        with mock.patch('apps.libraries.tape_operations_views._ltfs') as factory:
            factory.return_value.mount.return_value = failure_result(
                'Drive 0 holds no cartridge', ['Load one with `mhvtl op mount`'])
            response = self.client.post(
                reverse('libraries:ltfs_mount_ajax'),
                data=json.dumps({'library_id': 60, 'drive': 0}),
                content_type='application/json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertIn('no cartridge', body['message'])
        self.assertTrue(body['errors'])


class ProvisioningCardTests(TestCase):
    """The card that offers a drive, then cartridges.

    One thing at a time, in that order, and every value in it comes from
    libraries/workflow.ltfs_provisioning(). The view used to build the
    (vendor, model) pairs itself, which put the decision in a view; the tests
    that mocked that helper are these, rewritten against the service's shape.
    """

    def setUp(self):
        signed_in(self.client)

    def _get(self, drives, provisioning):
        from apps.libraries import tape_operations_views as views

        status = success_result('ok', {'drives': drives, 'tools': {},
                                       'format_allowed': False})
        with mock.patch.object(views, 'get_live_libraries', return_value=[]), \
             mock.patch('apps.libraries.tape_operations_views._ltfs') as factory, \
             mock.patch.object(views.LtfsView, '_provisioning',
                               return_value=provisioning):
            factory.return_value.status.return_value = status
            return self.client.get(reverse('libraries:ltfs') + '?library_id=50')

    @staticmethod
    def provisioning(**overrides):
        base = {
            'needs': [], 'can_add_drive': True, 'cannot_add_because': '',
            'can_add_media': True, 'cannot_add_media_because': '',
            'drive_candidates': [], 'media_candidates': [],
            'drive_slots': {'used': 2, 'max': 500},
            'storage_slots': {'empty': 4, 'full': 2, 'total': 6},
            'ltfs_media': 0, 'ltfs_media_barcodes': [],
        }
        base.update(overrides)
        return base

    def test_a_ready_library_still_gets_its_state_and_its_volumes(self):
        """It used to get nothing at all - no volume count, no slots, no
        statement that it was ready - which is the case where those numbers are
        most worth reading. Libraries 60 and 70 were blank pages."""
        response = self._get([{'drive_num': 0, 'ltfs_capable': True,
                               'model': 'ULT3580-TD7', 'vendor': 'IBM',
                               'revision': 'D.02'}],
                             self.provisioning(ltfs_media=3,
                                               ltfs_media_barcodes=['I60001L7',
                                                                    'I60002L7',
                                                                    'I60004L7']))
        self.assertContains(response, 'is ready for LTFS')
        self.assertNotContains(response, 'is not ready for LTFS')
        self.assertContains(response, '3 LTFS volumes')
        self.assertContains(response, 'I60001L7')
        self.assertContains(response, 'Drive slots')

    def test_a_ready_library_is_offered_no_drive(self):
        """The drive-first rule is unchanged: it already has one."""
        response = self._get([{'drive_num': 0, 'ltfs_capable': True,
                               'model': 'ULT3580-TD7', 'vendor': 'IBM',
                               'revision': 'D.02'}],
                             self.provisioning(ltfs_media=3))
        self.assertNotContains(response, 'value="add-drive"')

    def test_a_ready_library_may_still_be_given_more_cartridges(self):
        """Wanting more media is legitimate, and the wording says "more" rather
        than claiming it has none."""
        response = self._get(
            [{'drive_num': 0, 'ltfs_capable': True, 'model': 'ULT3580-TD7',
              'vendor': 'IBM', 'revision': 'D.02'}],
            self.provisioning(ltfs_media=3,
                              media_candidates=[{'density': 'LTO7',
                                                 'suffix': 'L7',
                                                 'usable': True,
                                                 'reason': ''}]))
        self.assertContains(response, 'value="add-media"')
        self.assertContains(response, 'More cartridges can be created')
        self.assertNotContains(response, 'and no LTFS volumes')

    def test_needing_a_drive_offers_the_drive_and_not_the_cartridges(self):
        """The order is the point: cartridges cannot be chosen until a drive
        exists, because the drive decides which densities are possible."""
        response = self._get(
            [{'drive_num': 0, 'ltfs_capable': False, 'model': 'ULT3580-TD8',
              'vendor': 'STK', 'revision': '0016'}],
            self.provisioning(
                needs=['drive'], ltfs_media=None,
                can_add_media=False,
                cannot_add_media_because='add a drive LTFS can open first',
                drive_candidates=[{'vendor': 'IBM', 'model': 'ULT3580-TD8',
                                   'firmware_minimum': 'HB81'}]))
        self.assertContains(response, 'not ready for LTFS')
        self.assertContains(response, 'IBM|ULT3580-TD8')
        self.assertContains(response, 'firmware HB81 or later')
        self.assertNotContains(response, 'value="add-media"')

    def test_unread_cartridges_are_said_to_be_unread_not_counted_as_zero(self):
        response = self._get([], self.provisioning(needs=['drive'],
                                                   ltfs_media=None))
        self.assertContains(response, 'LTFS volumes not read')

    def test_needing_cartridges_offers_them_with_the_densities_it_can_format(self):
        response = self._get(
            [{'drive_num': 0, 'ltfs_capable': True, 'model': 'ULT3580-TD7',
              'vendor': 'IBM', 'revision': 'D.02'}],
            self.provisioning(
                needs=['media'],
                media_candidates=[
                    {'density': 'LTO7', 'suffix': 'L7', 'usable': True,
                     'reason': ''},
                    {'density': 'LTO5', 'suffix': 'L5', 'usable': False,
                     'reason': 'this drive loads LTO5 read-only, and mkltfs writes'}]))
        self.assertContains(response, 'value="add-media"')
        self.assertContains(response, 'LTO7')
        self.assertNotContains(response, 'value="add-drive"')

    def test_a_density_it_cannot_format_is_shown_with_the_reason(self):
        response = self._get(
            [{'drive_num': 0, 'ltfs_capable': True, 'model': 'ULT3580-TD7',
              'vendor': 'IBM', 'revision': 'D.02'}],
            self.provisioning(
                needs=['media'],
                media_candidates=[
                    {'density': 'LTO7', 'suffix': 'L7', 'usable': True,
                     'reason': ''},
                    {'density': 'LTO5', 'suffix': 'L5', 'usable': False,
                     'reason': 'this drive loads LTO5 read-only'}]))
        self.assertContains(response, 'read-only')

    def test_a_library_that_cannot_take_a_capable_drive_says_why(self):
        response = self._get(
            [{'drive_num': 0, 'ltfs_capable': False, 'model': 'T10000B',
              'vendor': 'STK', 'revision': ''}],
            self.provisioning(
                needs=['drive'], can_add_drive=False, ltfs_media=None,
                cannot_add_because='no drive this library takes is one LTFS '
                                   'can open - it takes: T10000B'))
        self.assertContains(response, 'cannot be added')
        self.assertContains(response, 'T10000B')
        self.assertNotContains(response, 'value="add-drive"')

    def test_both_slot_counts_are_shown_before_a_choice_is_made(self):
        response = self._get([], self.provisioning(needs=['drive'],
                                                   ltfs_media=None))
        self.assertContains(response, 'Drive slots')
        self.assertContains(response, 'storage slots')

    def test_a_pair_that_was_not_offered_is_rejected(self):
        """The POST is checked against what the service offered, not trusted."""
        from apps.libraries import tape_operations_views as views

        with mock.patch.object(views.LtfsView, '_provisioning',
                               return_value=self.provisioning()), \
             mock.patch('apps.libraries.services.libraries.add_ltfs_drive_workflow') \
                as workflow:
            response = self.client.post(reverse('libraries:ltfs'), {
                'action': 'add-drive', 'library_id': 50,
                'pair': 'IBM|ULT3580-TD8'})
        workflow.assert_not_called()
        self.assertEqual(response.status_code, 302)

    def test_a_density_that_was_not_offered_is_rejected(self):
        from apps.libraries import tape_operations_views as views

        with mock.patch.object(views.LtfsView, '_provisioning',
                               return_value=self.provisioning()), \
             mock.patch('apps.libraries.services.libraries.add_ltfs_media_workflow') \
                as workflow:
            response = self.client.post(reverse('libraries:ltfs'), {
                'action': 'add-media', 'library_id': 50, 'count': 2,
                'density': 'LTO9'})
        workflow.assert_not_called()
        self.assertEqual(response.status_code, 302)

    def test_a_count_that_is_not_a_number_is_refused(self):
        from apps.libraries import tape_operations_views as views

        with mock.patch('apps.libraries.services.libraries.'
                        'add_ltfs_media_workflow') as workflow:
            response = self.client.post(reverse('libraries:ltfs'), {
                'action': 'add-media', 'library_id': 50, 'count': 'lots'},
                follow=True)
        workflow.assert_not_called()
        self.assertIn('must be a number',
                      ' '.join(m.message for m in response.context['messages']))

    def test_neither_action_is_reachable_through_the_actions_map(self):
        """Both are libraries workflows, not LtfsService methods."""
        from apps.libraries import tape_operations_views as views

        self.assertNotIn('add-drive', views.LtfsView.ACTIONS)
        self.assertNotIn('add-media', views.LtfsView.ACTIONS)

    def test_the_view_builds_no_candidates_of_its_own(self):
        """It used to pair vendors and models in the view. The decision belongs
        to the service, and the proof is that the view no longer imports the
        tables."""
        from apps.libraries import tape_operations_views as views

        source = Path(views.__file__).read_text()
        self.assertNotIn('_add_drive_options', source)
        self.assertNotIn('usable_drive_models', source)
