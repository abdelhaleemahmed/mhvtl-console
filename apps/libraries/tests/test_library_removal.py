"""Removing a library leaves every other library running.

The Remove Library page used to follow a successful delete with
restart_services() - no library id, so `systemctl restart mhvtl.target` -
which restarted every library on the host. The delete had already stopped the
removed library's own daemons, so the restart did nothing useful and two things
harmful: a job running on any other library lost its drive mid-write, and every
iSCSI export was left bound to devices that no longer existed, answering logins
with no LUNs and logging nothing. It went unnoticed through a whole day of
recordings, each of which ended with a removal.
"""
from unittest import mock

from django.urls import reverse

from apps.libraries import views
from apps.libraries.services.console import units
from apps.libraries.services.core import failure_result, success_result
from apps.libraries.services.libraries import LibraryService
from apps.libraries.tests.base import LibraryTestBase


class RemovalTests(LibraryTestBase):

    def setUp(self):
        super().setUp()
        # Both gates: the site-wide login middleware, and the view's own
        # mhvtl_logged_in session flag.
        self.login_as_user()
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()
        patches = {
            'available': mock.patch.object(views, 'MHVTL_SERVICE_AVAILABLE', True),
            'sync': mock.patch.object(views, '_sync_database'),
            'restart_services': mock.patch.object(LibraryService, 'restart_services'),
            'restart_library': mock.patch.object(units, 'restart_library'),
            'restart': mock.patch.object(units, 'restart'),
        }
        self.mocks = {name: patch.start() for name, patch in patches.items()}
        self.addCleanup(mock.patch.stopall)

    def remove(self, delete_result):
        with mock.patch.object(LibraryService, 'delete',
                               return_value=delete_result) as delete:
            response = self.client.post(reverse('libraries:remove'),
                                        {'library_id': '40', 'remove_media': 'YES'})
        return response, delete

    def test_a_removal_restarts_nothing(self):
        response, delete = self.remove(success_result('Library 40 deleted'))
        self.assertEqual(response.status_code, 302)
        delete.assert_called_once_with(40, force=False, remove_media=True)
        self.mocks['restart_services'].assert_not_called()
        self.mocks['restart_library'].assert_not_called()
        self.mocks['restart'].assert_not_called()

    def test_the_success_message_no_longer_claims_a_restart(self):
        self.remove(success_result('Library 40 deleted'))
        page = self.client.get(reverse('libraries:remove'))
        text = ' '.join(str(m) for m in page.context['messages'])
        self.assertIn('Library 40 deleted', text)
        self.assertNotIn('services restarted', text)

    def test_a_failed_removal_restarts_nothing_either(self):
        self.remove(failure_result('device.conf is not writable', ['denied']))
        self.mocks['restart_services'].assert_not_called()
        self.mocks['restart'].assert_not_called()
