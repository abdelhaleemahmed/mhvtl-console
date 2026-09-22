# apps/libraries/tests/test_views.py
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.libraries.models import Library, LibraryBrand, LibraryModel, Drive, MediaSlot
from apps.libraries.services.core import failure_result, success_result

User = get_user_model()


class LibraryViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', 'test@test.com', 'password')
        self.client.login(username='testuser', password='password')

        self.brand = LibraryBrand.objects.create(name='HP', display_name='HP Libraries')
        self.model = LibraryModel.objects.create(
            brand=self.brand,
            name='MSL G3 Series',
        )

    def test_dashboard_view(self):
        """Test library dashboard loads correctly"""
        response = self.client.get(reverse('libraries:dashboard'))
        # May redirect to setup if no libraries exist
        self.assertIn(response.status_code, [200, 302])

    def test_setup_choice_view(self):
        """Test setup choice page"""
        response = self.client.get(reverse('libraries:setup_choice'))
        self.assertIn(response.status_code, [200, 302])

    def test_brand_selection_view(self):
        """Test brand selection page"""
        response = self.client.get(reverse('libraries:brand_selection'))
        self.assertIn(response.status_code, [200, 302])

    def test_library_list_view(self):
        """Test library list page"""
        library = Library.objects.create(
            library_id=10,
            channel=0, target=0, lun=0,
            brand=self.brand,
            model=self.model,
            vendor_identification='HP',
            product_identification='MSL G3 Series',
        )
        response = self.client.get(reverse('libraries:list'))
        self.assertIn(response.status_code, [200, 302])


class LibraryDetailPageTests(TestCase):
    """The library page: the row it needs, and the tapes it never showed.

    A library made from the command line exists in device.conf before the
    database hears of it. The page answered 404 - "No Library matches the
    given query" - while the dashboard, which reads the files, showed the
    library perfectly well.
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.brand = LibraryBrand.objects.create(name='ADIC', display_name='ADIC')
        self.model = LibraryModel.objects.create(brand=self.brand, name='Scalar i2000')

    def _page(self) -> str:
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import views
        request = RequestFactory().get('/libraries/detail/40/')
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        return views.LibraryDetailView().get(request, 40).content.decode()

    def _row(self):
        return Library.objects.create(
            library_id=40, brand=self.brand, model=self.model,
            vendor_identification='ADIC', product_identification='Scalar i2000',
            unit_serial_number='XYZZY_40', channel=0, target=18, lun=0,
            is_active=True)

    def test_a_library_missing_from_the_database_is_synced_not_refused(self):
        from apps.libraries import views

        def create_it():
            self._row()
            return {}

        with self.mock.patch('apps.libraries.services.sync.service.'
                             'sync_mhvtl_to_django',
                             side_effect=create_it) as sync:
            row = views._library_row(40)
        sync.assert_called_once()
        self.assertEqual(row.library_id, 40)

    def test_a_library_that_is_there_is_not_synced(self):
        from apps.libraries import views
        self._row()
        with self.mock.patch('apps.libraries.services.sync.service.'
                             'sync_mhvtl_to_django') as sync:
            views._library_row(40)
        sync.assert_not_called()

    def test_a_library_no_sync_can_find_is_still_a_404(self):
        from django.http import Http404

        from apps.libraries import views
        with self.mock.patch('apps.libraries.services.sync.service.'
                             'sync_mhvtl_to_django', return_value={}):
            with self.assertRaises(Http404):
                views._library_row(99)

    def test_the_page_has_a_tapes_card_linked_to_the_inventory(self):
        """It showed the drives and never what was in the slots."""
        self._row()
        body = self._page()
        self.assertIn('Tapes (', body)
        self.assertIn('/libraries/operator/tapes/?library_id=40', body)

    def test_the_sync_button_has_the_script_that_defines_it(self):
        """Three buttons called syncLibrary(); the function was defined on two
        other pages, so every click here was a ReferenceError."""
        from django.utils import timezone
        row = self._row()
        row.discovery_status = 'discovered'
        row.last_synced = timezone.now()
        row.save()
        body = self._page()
        self.assertIn('syncLibrary(', body)
        self.assertIn('js/library-sync.js', body)

    def test_the_script_is_loaded_even_without_the_button(self):
        self._row()
        self.assertIn('js/library-sync.js', self._page())

    def test_the_page_is_a_hub_for_everything_about_this_library(self):
        """The work on one library was spread over the operator panel, and
        each of those pages then had to be told which library again. The page
        links to them with the library already chosen."""
        self._row()
        body = self._page()
        for group in ('Tapes', 'Drives', 'The robot', 'The library'):
            self.assertIn('>%s<' % group, body)
        for page in ('/libraries/operator/drives/add/?library_id=40',
                     '/libraries/operator/drives/remove/?library_id=40',
                     '/libraries/operator/tapes/create/?library_id=40',
                     '/libraries/operator/library-status/?library_id=40',
                     '/libraries/monitor/40/'):
            self.assertIn(page, body)

    def test_every_hub_link_carries_the_library(self):
        """A link without ?library_id lands on a page asking for a library
        again - the picker bug the operator pages had."""
        import re
        self._row()
        body = self._page()
        # class=, not the name: the stylesheet mentions .manage-grid first
        card = body.split('class="manage-grid"', 1)[1].split('detail-card', 1)[0]
        links = re.findall(r'href="(/libraries/[^"]+)"', card)
        self.assertGreater(len(links), 8)
        for link in links:
            if link == '/libraries/iscsi/':
                continue          # the exports page is not per library
            self.assertRegex(link, r'(\?library_id=40|/40/)', link)

    def test_the_drives_report_what_they_are_doing(self):
        """Live counters from `vtlcmd <drive> stats` - our MHVTL patch - so a
        drive that is writing says so while the backup runs.

        The panel arrives rendered and is replaced by a rendered one: the
        page asks the server for the fragment, and no state, wording or
        arithmetic happens in the browser, which is what lets
        `mhvtl status activity` print the same thing."""
        self._row()
        body = self._page()
        self.assertIn('data-refresh-url="/libraries/activity/40/"', body)
        self.assertIn('data-refresh-seconds="5"', body)
        self.assertIn('js/auto-refresh.js', body)
        self.assertNotIn('library-activity.js', body)

    def test_the_page_does_no_arithmetic_of_its_own(self):
        """Anything the page computes is a feature the command line does not
        have."""
        from pathlib import Path
        template = (Path(__file__).resolve().parents[1]
                    / 'templates/libraries/library_detail.html').read_text()
        script = template.split('{% block extra_js %}')[-1]
        for computing in ('1024', 'toFixed', 'Math.', 'JSON.parse'):
            self.assertNotIn(computing, script)


class CreateWizardDefaultTests(TestCase):
    """The create form's starting counts come from the vendor profile.

    Empty Slots was written into the template as 40, which is why a new
    library offered forty spare slots while the command line gave four.
    """

    def setUp(self):
        LibraryBrand.objects.create(name='ADIC', display_name='ADIC')

    def test_the_form_starts_from_the_profile_defaults(self):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import views
        from apps.libraries.services.profiles.data import get_profile_options

        defaults = get_profile_options('ADIC')['defaults']
        request = RequestFactory().get('/libraries/setup/brand/ADIC/')
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        body = views.BrandConfigView().get(request, 'ADIC').content.decode()
        self.assertIn(f'id="emptySlots" value="{defaults["empty_slots"]}"', body)
        self.assertIn(f'id="mediaCount" value="{defaults["media_count"]}"', body)

    def test_the_catalogue_carries_the_empty_slot_default(self):
        from apps.libraries.services.profiles import data
        for key in data.list_profiles():
            with self.subTest(profile=key):
                defaults = data.get_profile_options(key)['defaults']
                self.assertEqual(defaults['empty_slots'],
                                 data.get_profile(key).default_empty_slots)


class LibraryControlViewTests(TestCase):
    """Stop, start, restart, and the empty-slot change.

    The detail page had posted to /libraries/control/<id>/ since it was
    written, with the URL commented out and no view behind it: the Stop button
    answered 404.
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock
        brand = LibraryBrand.objects.create(name='ADIC', display_name='ADIC')
        model = LibraryModel.objects.create(brand=brand, name='Scalar i2000')
        Library.objects.create(library_id=40, brand=brand, model=model,
                               vendor_identification='ADIC',
                               product_identification='Scalar i2000',
                               unit_serial_number='XYZZY_40',
                               channel=0, target=18, lun=0, is_active=True)

    def _post(self, data):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import views
        request = RequestFactory().post('/libraries/control/40/', data)
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        response = views.LibraryControlView().post(request, 40)
        return response, [str(m) for m in request._messages]

    def test_the_control_url_exists(self):
        self.assertEqual(reverse('libraries:library_control', args=[40]),
                         '/libraries/control/40/')

    def test_stop_stops_the_library_and_its_drives(self):
        from apps.libraries.services.console import units
        with self.mock.patch.object(units, 'stop_library',
                                    return_value={'vtllibrary@40.service': True,
                                                  'vtltape@41.service': True}) as stop:
            response, notes = self._post({'action': 'stop'})
        stop.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('2 unit(s) stopped' in note for note in notes), notes)

    def test_a_unit_that_will_not_stop_is_reported(self):
        from apps.libraries.services.console import units
        with self.mock.patch.object(units, 'stop_library',
                                    return_value={'vtltape@41.service': False}):
            _, notes = self._post({'action': 'stop'})
        self.assertTrue(any('did not stop' in note for note in notes), notes)

    def test_slots_are_changed_through_the_service(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import lifecycle
        with self.mock.patch.object(
                lifecycle, 'set_empty_slots',
                return_value=success_result('Library 40 now has 8 slots')) as change, \
             self.mock.patch.object(units, 'restart_library') as restart:
            _, notes = self._post({'action': 'slots', 'empty_slots': '6'})
        self.assertEqual(change.call_args[0][:2], (40, 6))
        restart.assert_not_called()
        self.assertTrue(any('Restart the library' in note for note in notes), notes)

    def test_the_restart_box_restarts_it(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import lifecycle
        with self.mock.patch.object(lifecycle, 'set_empty_slots',
                                    return_value=success_result('ok')), \
             self.mock.patch.object(units, 'restart_library',
                                    return_value={'ok': True,
                                                  'restarted': 'vtllibrary@40.service'}) as restart:
            _, notes = self._post({'action': 'slots', 'empty_slots': '6',
                                   'restart': 'on'})
        restart.assert_called_once_with(40)
        self.assertTrue(any('Restarted' in note for note in notes), notes)

    def test_a_refused_slot_change_says_why_and_restarts_nothing(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import lifecycle
        with self.mock.patch.object(
                lifecycle, 'set_empty_slots',
                return_value=failure_result('cannot go down to 0 empty slots',
                                            ['a gap would shorten the library'])), \
             self.mock.patch.object(units, 'restart_library') as restart:
            _, notes = self._post({'action': 'slots', 'empty_slots': '0',
                                   'restart': 'on'})
        restart.assert_not_called()
        self.assertTrue(any('gap' in note for note in notes), notes)

    def test_an_unknown_action_changes_nothing(self):
        from apps.libraries.services.console import units
        with self.mock.patch.object(units, 'stop_library') as stop:
            _, notes = self._post({'action': 'sing'})
        stop.assert_not_called()
        self.assertTrue(any('Unknown action' in note for note in notes), notes)


class ConfigureDisabledTests(TestCase):
    """The configure page is disabled until it is redesigned.

    Its Save posted vendor, product and serial to LibraryService.update(),
    which deletes and recreates the library - MHVTL reads those strings once,
    when the daemon starts - and the page said nothing about it.
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock
        brand = LibraryBrand.objects.create(name='ADIC', display_name='ADIC')
        model = LibraryModel.objects.create(brand=brand, name='Scalar i2000')
        Library.objects.create(library_id=40, brand=brand, model=model,
                               vendor_identification='ADIC',
                               product_identification='Scalar i2000',
                               unit_serial_number='XYZZY_40',
                               channel=0, target=18, lun=0, is_active=True)

    def _call(self, method, data=None):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import views
        factory = RequestFactory()
        request = (factory.post('/libraries/configure/40/', data or {})
                   if method == 'post' else factory.get('/libraries/configure/40/'))
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        view = views.LibraryConfigureView()
        response = getattr(view, method)(request, 40)
        return response, [str(m) for m in request._messages]

    def test_opening_it_sends_you_to_the_library_page_with_the_reason(self):
        response, notes = self._call('get')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/libraries/detail/40/')
        self.assertTrue(any('disabled in this release' in note for note in notes), notes)

    def test_a_post_changes_nothing(self):
        """A stale page, or a script that has not noticed."""
        from apps.libraries.services.libraries import LibraryService
        with self.mock.patch.object(LibraryService, 'update') as update:
            response, notes = self._call('post', {'vendor_identification': 'STK'})
        update.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('Nothing was changed' in note for note in notes), notes)

    def test_no_page_links_to_it_any_more(self):
        from pathlib import Path
        templates = Path('apps/libraries/templates')
        linking = [str(path) for path in templates.rglob('*.html')
                   if 'library_configure' in path.read_text()
                   or '/libraries/configure/' in path.read_text()]
        self.assertEqual(linking, [], f'still linking to the disabled page: {linking}')


class DriveActivityPanelTests(TestCase):
    """The live drive panel, on both pages that should have it.

    The monitor page is the one called Monitor and was the only place that
    never said what a drive was doing. It and the library page now render the
    same server-built panel and refresh it the same way.
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.brand = LibraryBrand.objects.create(name='STK', display_name='STK')
        self.model = LibraryModel.objects.create(brand=self.brand, name='SL500')
        Library.objects.create(
            library_id=50, brand=self.brand, model=self.model,
            vendor_identification='STK', product_identification='SL500',
            unit_serial_number='XYZZY_50', channel=0, target=18, lun=0,
            is_active=True)

    def _page(self, path, view_name):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import views
        request = RequestFactory().get(path)
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        rows = [{'drive_id': 51, 'state': 'writing', 'barcode': 'K50001L8',
                 'label': 'writing K50001L8',
                 'detail': '600.0 MB written - 59.2% of the tape'},
                {'drive_id': 52, 'state': 'empty', 'barcode': None,
                 'label': 'empty', 'detail': ''}]
        with self.mock.patch.object(views, '_drive_activity', return_value=rows):
            view = getattr(views, view_name)()
            return view.get(request, 50).content.decode()

    def test_the_monitor_page_says_what_the_drives_are_doing(self):
        body = self._page('/libraries/monitor/50/', 'LibraryMonitorView')
        self.assertIn('What the drives are doing', body)
        self.assertIn('writing K50001L8', body)
        self.assertIn('600.0 MB written', body)

    def test_the_panel_draws_a_bar_for_a_loaded_drive(self):
        from django.template.loader import render_to_string
        html = render_to_string('libraries/partials/_library_activity.html',
                                {'drives': [{'drive_id': 51, 'state': 'writing',
                                             'label': 'writing K50001L8',
                                             'detail': '363.0 MB written',
                                             'percent': 75.6,
                                             'fullness': 'filling'}]})
        self.assertIn('tape-bar live-bar filling', html)
        self.assertIn('width: 75.6%', html)

    def test_a_drive_with_no_tape_gets_no_bar(self):
        from django.template.loader import render_to_string
        html = render_to_string('libraries/partials/_library_activity.html',
                                {'drives': [{'drive_id': 52, 'state': 'empty',
                                             'label': 'empty', 'detail': '',
                                             'percent': None,
                                             'fullness': 'unknown'}]})
        self.assertNotIn('tape-bar', html)

    def test_the_monitor_panel_refreshes_itself(self):
        body = self._page('/libraries/monitor/50/', 'LibraryMonitorView')
        self.assertIn('data-refresh-url="/libraries/activity/50/"', body)
        self.assertIn('js/auto-refresh.js', body)

    def test_the_library_page_has_the_same_panel(self):
        body = self._page('/libraries/detail/50/', 'LibraryDetailView')
        self.assertIn('writing K50001L8', body)
        self.assertIn('data-refresh-url="/libraries/activity/50/"', body)

    def test_both_pages_are_filled_in_on_arrival(self):
        """It used to render a line per drive with nothing on it, and stayed
        that way until the first refresh five seconds later."""
        for path, view in (('/libraries/monitor/50/', 'LibraryMonitorView'),
                           ('/libraries/detail/50/', 'LibraryDetailView')):
            with self.subTest(view=view):
                body = self._page(path, view)
                panel = body.split('class="drive-activity"', 1)[1]
                panel = panel.split('</div>\n</div>', 1)[0]
                self.assertIn('writing K50001L8', panel)

    def test_a_library_whose_daemons_are_stopped_still_renders(self):
        """activity() reaching nothing must never fail a page."""
        from apps.libraries import views
        with self.mock.patch.object(views.DriveService, 'activity',
                                    side_effect=OSError('no queue')):
            self.assertEqual(views._drive_activity(50), [])


class TapeTileTests(TestCase):
    """Tapes drawn the way a file manager draws drives.

    The card listed slot, barcode, type and density and said nothing about
    the one thing a tape runs out of. The tile shows how full it is, and the
    service decides the percentage, the wording and how alarming it looks -
    so `mhvtl tape list` and this page describe a tape the same way.
    """

    def _render(self, tapes):
        from django.template.loader import render_to_string
        return render_to_string('libraries/partials/_tape_tiles.html',
                                {'tapes': tapes})

    def _tape(self, **fields):
        from apps.libraries.services.tapes.models import TapeInfo
        return TapeInfo(**{'barcode': 'K50001L8', 'library_id': 50, 'slot': 1,
                           'media_exists': True, **fields}).to_dict()

    def test_a_tile_shows_how_full_the_tape_is(self):
        html = self._render([self._tape(used_mb=363, capacity_mb=480)])
        self.assertIn('K50001L8', html)
        self.assertIn('117.0 MB free of 480.0 MB', html)
        self.assertIn('width: 75.6%', html)

    def test_a_full_tape_looks_different_from_an_empty_one(self):
        full = self._render([self._tape(used_mb=470, capacity_mb=480)])
        empty = self._render([self._tape(used_mb=0, capacity_mb=480)])
        self.assertIn('tape-tile full', full)
        self.assertIn('tape-tile normal', empty)
        self.assertIn('480.0 MB free of 480.0 MB', empty)

    def test_a_tape_that_is_filling_up_is_marked_before_it_is_full(self):
        html = self._render([self._tape(used_mb=380, capacity_mb=480)])
        self.assertIn('tape-tile filling', html)

    def test_a_tape_whose_size_is_unknown_says_so(self):
        """Better than a bar at some invented width."""
        html = self._render([self._tape(used_mb=None, capacity_mb=None)])
        self.assertIn('size not known', html)
        self.assertIn('tape-tile unknown', html)

    def test_the_generation_is_coloured_by_the_service(self):
        """That rule lived in JavaScript on the mount page, so a second page
        would have needed a second copy of it."""
        html = self._render([self._tape(barcode='K50001L7')])
        self.assertIn('lto-7', html)
        self.assertIn('>LTO7<', html)

    def test_the_template_works_nothing_out_for_itself(self):
        from pathlib import Path
        template = (Path(__file__).resolve().parents[1]
                    / 'templates/libraries/partials/_tape_tiles.html').read_text()
        for computing in ('|add:', '|divisibleby', 'widthratio', '|lower'):
            self.assertNotIn(computing, template)

    def test_a_tape_with_no_data_on_disk_is_flagged(self):
        html = self._render([self._tape(media_exists=False)])
        self.assertIn('no data on disk', html)

    def test_the_library_page_shows_the_tiles(self):
        from unittest import mock

        from apps.libraries import views
        brand = LibraryBrand.objects.create(name='STK', display_name='STK')
        model = LibraryModel.objects.create(brand=brand, name='SL500')
        Library.objects.create(
            library_id=50, brand=brand, model=model,
            vendor_identification='STK', product_identification='SL500',
            unit_serial_number='XYZZY_50', channel=0, target=18, lun=0,
            is_active=True)

        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        request = RequestFactory().get('/libraries/detail/50/')
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()

        listed = success_result('1 tape', {'tapes': [
            self._tape(used_mb=363, capacity_mb=480)]}, 'op')
        with mock.patch.object(views.TapeService, 'list', return_value=listed), \
                mock.patch.object(views, '_drive_activity', return_value=[]):
            body = views.LibraryDetailView().get(request, 50).content.decode()
        self.assertIn('tape-tile', body)
        self.assertIn('117.0 MB free of 480.0 MB', body)
