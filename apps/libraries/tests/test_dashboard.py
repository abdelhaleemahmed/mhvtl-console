"""Dashboard summary: the data behind the overview tiles.

The old dashboard hardcoded "System Status: Online" in the template, so it
claimed the system was up while every daemon was dead. These tests cover the
replacement, and in particular that it never invents a number it does not have.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.authentication.models import DEFAULT_PASSWORD
from apps.libraries.services.dashboard import service as dashboard_service

LIBRARY_CONTENTS = """VERSION: 2

Drive 1:
Drive 2:

Picker 1:

MAP 1:

Slot 1: E01001L8
Slot 2: E01002L8
Slot 3: CLN101L8
Slot 4:
"""


class MediaSummaryTests(TestCase):
    def _with_config(self, files):
        tmp = tempfile.mkdtemp()
        for name, content in files.items():
            Path(tmp, name).write_text(content)
        return tmp

    def test_counts_occupied_slots_and_cleaning_tapes(self):
        config = self._with_config({'library_contents.10': LIBRARY_CONTENTS})
        with override_settings(MHVTL_CONFIG_DIR=config):
            data = dashboard_service._media_summary()

        self.assertEqual(data['total'], 3)          # Slot 4 is empty
        self.assertEqual(data['cleaning'], 1)
        self.assertEqual(data['data_tapes'], 2)
        self.assertEqual(data['per_library'], {10: 3})
        self.assertTrue(data['known'])
        self.assertFalse(data['partial'])

    def test_several_libraries_are_totalled(self):
        config = self._with_config({
            'library_contents.10': LIBRARY_CONTENTS,
            'library_contents.20': LIBRARY_CONTENTS,
            'library_contents.notanumber': LIBRARY_CONTENTS,
        })
        with override_settings(MHVTL_CONFIG_DIR=config):
            data = dashboard_service._media_summary()

        self.assertEqual(data['total'], 6)
        self.assertEqual(sorted(data['per_library']), [10, 20])

    def test_no_config_files_reports_unknown_not_zero(self):
        """A confident 0 would be the same mistake as the old hardcoded tile."""
        with override_settings(MHVTL_CONFIG_DIR=self._with_config({})):
            data = dashboard_service._media_summary()

        self.assertIsNone(data['total'])
        self.assertFalse(data['known'])

    def test_unreadable_file_is_reported_not_skipped_silently(self):
        config = self._with_config({
            'library_contents.10': LIBRARY_CONTENTS,
            'library_contents.20': LIBRARY_CONTENTS,
        })
        Path(config, 'library_contents.20').chmod(0o000)

        with override_settings(MHVTL_CONFIG_DIR=config):
            data = dashboard_service._media_summary()

        # Running as root defeats the permission check; only assert when it bites.
        if data['unreadable']:
            self.assertEqual(data['unreadable'], ['library_contents.20'])
            self.assertTrue(data['partial'])
            self.assertEqual(data['per_library'], {10: 3})


class SummaryResilienceTests(TestCase):
    """One failing collector must not take the whole dashboard down."""

    def test_failing_section_is_isolated_and_reported(self):
        with mock.patch.object(dashboard_service, '_service_status',
                               side_effect=OSError('systemctl not found')):
            summary = dashboard_service.get_dashboard_summary()

        self.assertFalse(summary['service']['ok'])
        self.assertIn('systemctl not found', summary['service']['error'])
        self.assertEqual(summary['degraded'], ['service'])
        self.assertTrue(summary['libraries']['ok'], 'other sections should still be gathered')
        self.assertTrue(any('service state' in a['message'] for a in summary['alerts']))

    def test_stopped_service_raises_alerts(self):
        stopped = {
            'running': False, 'enabled': True, 'state': 'inactive', 'healthy': False,
            'module_loaded': False, 'all_required_modules': False,
            'daemons_active': 0, 'daemons_total': 15,
            'libraries_active': 0, 'libraries_total': 3,
            'drives_active': 0, 'drives_total': 12,
        }
        with mock.patch.object(dashboard_service, '_service_status', return_value=stopped):
            summary = dashboard_service.get_dashboard_summary()

        messages = ' '.join(a['message'] for a in summary['alerts'])
        self.assertIn('kernel module is not loaded', messages)
        self.assertIn('daemons are not running', messages)
        self.assertTrue(all(a['level'] == 'danger'
                            for a in summary['alerts']
                            if 'mhvtl' in a['message']))


class SummaryEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse('libraries:dashboard_summary_ajax')

    def test_requires_login(self):
        response = self.client.get(self.url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)

    def test_returns_all_sections(self):
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        response = self.client.get(self.url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        for section in ('service', 'libraries', 'media', 'storage'):
            self.assertIn(section, payload['summary'])
            self.assertIn('ok', payload['summary'][section])
        self.assertIn('alerts', payload['summary'])


class DashboardPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})

    def test_page_renders_without_waiting_for_data(self):
        response = self.client.get(reverse('authentication:dashboard'))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('tile-service', body)
        self.assertIn('skeleton', body, 'tiles should render as placeholders first')

    def test_page_no_longer_hardcodes_a_status(self):
        """The old template asserted "Online" and "Django 4.2" in markup."""
        body = self.client.get(reverse('authentication:dashboard')).content.decode()
        self.assertNotIn('Django 4.2', body)
        self.assertNotIn('>Online<', body)


class RootOnlyConfigTests(TestCase):
    """A default MHVTL install ships device.conf as mode 0600.

    Both dashboard collectors used to read the config directory directly - one
    with a glob and read_text(), the other through an adapter - so on a host
    where the files are not world-readable the dashboard reported no libraries
    and no tapes. They go through ConfigService now, which falls back to sudo.
    This was found when /etc/mhvtl was tightened to 2750 root:mhvtl, not by a
    test, which is why these exist.
    """

    def test_media_counts_come_through_the_config_service(self):
        from apps.libraries.services.config.service import ConfigService

        with mock.patch.object(ConfigService, 'library_contents') as read:
            with mock.patch.object(ConfigService, 'files', return_value=[]):
                dashboard_service._media_summary()
        # No direct file access: with files() empty nothing is read at all.
        read.assert_not_called()

    def test_a_library_it_cannot_read_is_reported_not_dropped(self):
        """A library counted as zero tapes is worse than one reported unknown."""
        from apps.libraries.services.config.service import ConfigService

        class Entry:
            def __init__(self, name):
                self.name = name

        from apps.libraries.services.config import library_contents

        def read(library_id):
            return (None if library_id == 20
                    else library_contents.parse(LIBRARY_CONTENTS))

        with mock.patch.object(ConfigService, 'files',
                               return_value=[Entry('library_contents.10'),
                                             Entry('library_contents.20')]), \
             mock.patch.object(ConfigService, 'library_contents',
                               side_effect=read):
            data = dashboard_service._media_summary()

        self.assertEqual(data['unreadable'], ['library_contents.20'])
        self.assertTrue(data['partial'])
        self.assertEqual(data['per_library'], {10: 3})

    def test_libraries_are_read_through_the_library_service(self):
        from apps.libraries.services.libraries import LibraryService
        from apps.libraries.services.core import success_result

        payload = success_result('2 libraries', {'libraries': [
            {'library_id': 10, 'vendor': 'STK', 'product': 'L700',
             'serial': 'XYZZY_A', 'slot_count': 39, 'drives': 4},
            {'library_id': 20, 'vendor': 'SONY', 'product': 'LIB-302',
             'serial': '80000020', 'slot_count': None, 'drives': 4}]})

        with mock.patch.object(LibraryService, 'list', return_value=payload):
            data = dashboard_service._library_summary()

        self.assertEqual(data['count'], 2)
        self.assertEqual(data['total_drives'], 8)
        self.assertEqual([lib['status'] for lib in data['libraries']],
                         ['active', 'missing_contents'])

    def test_an_unreadable_device_conf_degrades_the_section(self):
        """Rather than reporting a host with no libraries."""
        from apps.libraries.services.libraries import LibraryService
        from apps.libraries.services.core import failure_result

        with mock.patch.object(LibraryService, 'list',
                               return_value=failure_result(
                                   'cannot read device.conf', ['permission denied'])):
            summary = dashboard_service.get_dashboard_summary()

        self.assertIn('libraries', summary['degraded'])
        self.assertFalse(summary['libraries']['ok'])
