"""The operator views, on services directly.

tape_operations_views.py used to go through adapters/tape_operations_service
and adapters/mhvtl_library_service. These check the shapes its templates and
JavaScript read, now built in the views module, and the bugs found on the
way: the library status page and its endpoint - and the mount page's
library-status-lto endpoint in the adapter - raised AttributeError on a
picker_count mtx never had.

mtx, mt and lsscsi are faked; nothing here touches a device.
"""
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from apps.libraries import tape_operations_views as views
from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.operations import mt, mtx
from apps.libraries.services.scsi import lsscsi, mapping
from apps.libraries.services.tapes import TapeService, media

FIXTURES = Path(__file__).parent / 'fixtures'


def _request(method='get', data=None, body=None):
    factory = RequestFactory()
    if body is not None:
        request = factory.post('/', data=json.dumps(body), content_type='application/json')
    elif method == 'post':
        request = factory.post('/', data or {})
    else:
        request = factory.get('/', data or {})
    request.session = {'mhvtl_logged_in': True}
    request._messages = FallbackStorage(request)
    request.user = AnonymousUser()
    return request


class FixtureConfigMixin:

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in ('device.conf', 'library_contents.10', 'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.status = mtx.parse((FIXTURES / 'mtx-status-lib10.txt').read_text())
        self.media = Path(tempfile.mkdtemp())
        override = self.settings(MHVTL_CONFIG_DIR=str(self.config),
                                 MHVTL_HOME_DIR=str(self.media))
        override.enable()
        self.addCleanup(override.disable)


class LibraryStatusTests(FixtureConfigMixin, TestCase):

    def _patched(self):
        return (mock.patch.object(mapping, 'device_for_library', return_value='/dev/sg4'),
                mock.patch.object(mtx, 'status', return_value=self.status))

    def test_the_status_shape(self):
        a, b = self._patched()
        with a, b:
            status = views._library_status(10)
        self.assertTrue(status['success'])
        self.assertEqual(status['device_path'], '/dev/sg4')
        self.assertEqual(status['slot_summary']['loaded_drives'], 1)
        self.assertEqual(status['drives'][0],
                         {'drive_num': 0, 'barcode': 'E01003L8', 'full': True,
                          'slot_origin': 3})
        self.assertEqual(len(status['import_export_slots']), 4)

    def test_the_status_endpoint_answers(self):
        """It raised AttributeError (picker_count) and returned a 500."""
        a, b = self._patched()
        with a, b:
            response = views.library_status_ajax(_request(), 10)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)['success'])

    def test_a_library_with_no_device_says_so(self):
        with mock.patch.object(mapping, 'device_for_library', return_value=None):
            status = views._library_status(10)
        self.assertFalse(status['success'])
        self.assertIn('Could not find device', status['error'])
        self.assertEqual(status['slot_summary']['total_slots'], 0)


class DriveStatusTests(TestCase):

    def test_the_drive_shape(self):
        state = mt.parse('/dev/nst0', (FIXTURES / 'mt-status-drive.txt').read_text())
        with mock.patch.object(mapping, 'device_for_drive', return_value='/dev/nst0'), \
                mock.patch.object(mt, 'status', return_value=state):
            response = views.drive_status_ajax(_request(), 11)
        status = json.loads(response.content)
        self.assertTrue(status['success'])
        self.assertTrue(status['online'])
        self.assertFalse(status['tape_loaded'])
        self.assertEqual(status['density'], 'LTO-8')


class DiscoveryTests(TestCase):

    def test_robots_and_tapes_are_split(self):
        devices = lsscsi.parse((FIXTURES / 'lsscsi-g.txt').read_text())
        with mock.patch.object(lsscsi, 'discover', return_value=devices):
            answer = json.loads(views.discover_devices_ajax(_request()).content)
        self.assertEqual((len(answer['robots']), len(answer['tapes'])), (3, 12))
        self.assertEqual(answer['robots'][0]['host'], '[16:0:0:0]')
        self.assertEqual(answer['robots'][0]['generic_path'], '/dev/sg4')


class MovementTests(FixtureConfigMixin, TestCase):
    """The movement endpoints reach OperationsService, including its check
    that the drive loads the cartridge."""

    def _run(self, view, body):
        with mock.patch.object(mapping, 'device_for_library', return_value='/dev/sg4'), \
                mock.patch.object(mtx, 'status', return_value=self.status), \
                mock.patch.object(mtx, 'load', return_value=CommandResult([], 0, '', '')) as load, \
                mock.patch.object(mtx, 'unload', return_value=CommandResult([], 0, '', '')):
            answer = json.loads(view(_request(body=body)).content)
        return answer, load

    def test_an_incompatible_mount_is_refused(self):
        """Slot 30 holds F01030L6; mtx drive 1 is an ULT3580-TD8."""
        answer, load = self._run(views.mount_tape_ajax,
                                 {'library_id': 10, 'slot': 30, 'drive': 1})
        self.assertFalse(answer['success'])
        self.assertIn('too old', answer['message'])
        load.assert_not_called()

    def test_a_compatible_mount_goes_through(self):
        answer, load = self._run(views.mount_tape_ajax,
                                 {'library_id': 10, 'slot': 30, 'drive': 3})
        self.assertTrue(answer['success'], answer['message'])
        self.assertIn('command_output', answer)
        load.assert_called_once()

    def test_unmount_passes_drive_and_slot_in_the_right_order(self):
        answer, _ = self._run(views.unmount_tape_ajax,
                              {'library_id': 10, 'slot': 3, 'drive': 0})
        self.assertTrue(answer['success'], answer['message'])
        self.assertEqual(answer['data']['slot'], 3)


class TapeHelperTests(FixtureConfigMixin, TestCase):

    def test_delete_by_slot_resolves_the_barcode(self):
        with mock.patch.object(TapeService, 'delete', autospec=True) as delete:
            views._delete_tape(10, None, 1, False)
        self.assertEqual(delete.call_args.args[1:], (10, 'E01001L8'))
        self.assertEqual(delete.call_args.kwargs, {'remove_media': False})

    def test_delete_by_an_empty_slot_is_refused(self):
        empty = next(s.number for s in __import__(
            'apps.libraries.services.config.library_contents', fromlist=['parse']
        ).parse((self.config / 'library_contents.10').read_text()).slots if not s.full)
        with mock.patch.object(TapeService, 'delete') as delete:
            result = views._delete_tape(10, None, empty, False)
        self.assertFalse(result.success)
        self.assertIn('empty', result.message)
        delete.assert_not_called()

    def test_next_barcode_endpoint_shape(self):
        answer = json.loads(views.next_barcode_ajax(
            _request(data={'prefix': 'E01', 'suffix': 'L8'}), 10).content)
        self.assertEqual(answer['next_barcode'], 'E01021L8')
        self.assertEqual(answer['detected_suffix'], 'L8')
        for key in ('next_number', 'existing_count', 'consecutive_available',
                    'prefix', 'suffix'):
            self.assertIn(key, answer)

    def test_validate_barcode_endpoint(self):
        taken = json.loads(views.validate_barcode_ajax(
            _request(data={'barcode': 'E01001L8'}), 10).content)
        free = json.loads(views.validate_barcode_ajax(
            _request(data={'barcode': 'E01099L8'}), 10).content)
        bad = json.loads(views.validate_barcode_ajax(
            _request(data={'barcode': '../ETC'}), 10).content)
        self.assertEqual((taken['valid'], free['valid'], bad['valid']),
                         (False, True, False))

    def test_slot_stats(self):
        stats = views._slot_stats(10)
        self.assertEqual(stats['total'], stats['occupied'] + stats['free'])
        self.assertEqual(stats['next_slot'], 21)

    def test_drives_endpoint_shape(self):
        answer = json.loads(views.list_drives_ajax(_request(), 10).content)
        self.assertEqual(answer['count'], 4)
        self.assertEqual(sorted(answer['drives'][0]),
                         ['channel', 'drive_id', 'lun', 'product', 'serial',
                          'slot', 'target', 'vendor'])

    def test_create_endpoint_leaves_the_density_to_the_service(self):
        """It defaulted to LTO8 whatever the library held."""
        with mock.patch.object(media, 'create',
                               return_value=CommandResult([], 0, '', '')) as mktape, \
                mock.patch.object(views, '_tapes',
                                  return_value=TapeService(self.config, self.media)):
            answer = json.loads(views.create_tape_ajax(_request(body={
                'library_id': 30, 'barcode': 'K0309999', 'slot': 1})).content)
        # Library 30 is full, so the create is refused - but not for density.
        self.assertNotIn('density', answer['message'].lower())
        mktape.assert_not_called()


class AdoptTapeViewTests(FixtureConfigMixin, TestCase):
    """The tape inventory page's 'tapes on disk with no library' section.

    The service refuses what cannot be adopted; these are about the page: that
    the loose tapes reach the template, that the form's library and slot are
    passed on, and that the restart is offered rather than assumed.
    """

    def setUp(self):
        super().setUp()
        (self.media / 'E01099L8').mkdir()
        (self.media / 'E01099L8' / 'data.0').write_bytes(b'')

    def test_the_inventory_lists_a_tape_no_library_claims(self):
        with mock.patch.object(views, 'get_live_libraries', return_value=[]), \
             mock.patch.object(media, 'usage_for_all', return_value={}):
            response = views.TapeListView().get(_request())
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Tapes on disk with no library', body)
        self.assertIn('E01099L8', body)

    def test_a_tape_a_library_lists_is_not_offered(self):
        (self.media / 'E01001L8').mkdir()          # library 10 holds this one
        with mock.patch.object(views, 'get_live_libraries', return_value=[]), \
             mock.patch.object(media, 'usage_for_all', return_value={}):
            body = views.TapeListView().get(_request()).content.decode()
        table = body.split('Tapes on disk with no library')[-1]
        self.assertNotIn('E01001L8', table)

    def test_adopting_passes_the_library_and_slot_through(self):
        request = _request('post', {'barcode': 'E01099L8', 'library_id': '10',
                                    'slot': '24'})
        with mock.patch.object(TapeService, 'adopt') as adopt:
            adopt.return_value = mock.Mock(success=True, message='done',
                                           data={'slot': 24})
            response = views.AdoptTapeView().post(request)
        self.assertEqual(adopt.call_args[0][:2], (10, 'E01099L8'))
        self.assertEqual(adopt.call_args[1]['slot'], 24)
        self.assertEqual(response.status_code, 302)
        self.assertIn('library_id=10', response.url)

    def test_no_slot_means_the_first_free_one(self):
        request = _request('post', {'barcode': 'E01099L8', 'library_id': '10'})
        with mock.patch.object(TapeService, 'adopt') as adopt:
            adopt.return_value = mock.Mock(success=True, message='done', data={})
            views.AdoptTapeView().post(request)
        self.assertIsNone(adopt.call_args[1]['slot'])

    def test_restart_is_done_when_asked(self):
        request = _request('post', {'barcode': 'E01099L8', 'library_id': '10',
                                    'restart': 'on'})
        with mock.patch.object(TapeService, 'adopt') as adopt, \
             mock.patch.object(views.units, 'restart_library',
                               return_value={'ok': True,
                                             'restarted': 'vtllibrary@10.service'}) as restart:
            adopt.return_value = mock.Mock(success=True, message='done', data={})
            views.AdoptTapeView().post(request)
        restart.assert_called_once_with(10)

    def test_without_restart_the_page_says_how(self):
        request = _request('post', {'barcode': 'E01099L8', 'library_id': '10'})
        with mock.patch.object(TapeService, 'adopt') as adopt, \
             mock.patch.object(views.units, 'restart_library') as restart:
            adopt.return_value = mock.Mock(success=True, message='done', data={})
            views.AdoptTapeView().post(request)
        restart.assert_not_called()
        notes = [str(m) for m in request._messages]
        self.assertTrue(any('Restart the library' in note for note in notes), notes)

    def test_a_failed_restart_does_not_hide_that_the_tape_is_back(self):
        request = _request('post', {'barcode': 'E01099L8', 'library_id': '10',
                                    'restart': 'on'})
        with mock.patch.object(TapeService, 'adopt') as adopt, \
             mock.patch.object(views.units, 'restart_library',
                               return_value={'ok': False, 'error': 'unit failed',
                                             'restarted': 'vtllibrary@10.service'}):
            adopt.return_value = mock.Mock(success=True, message='E01099L8 is back',
                                           data={})
            views.AdoptTapeView().post(request)
        notes = [str(m) for m in request._messages]
        self.assertTrue(any('E01099L8 is back' in note for note in notes), notes)
        self.assertTrue(any('did not restart' in note for note in notes), notes)

    def test_a_refusal_is_reported_and_nothing_is_restarted(self):
        request = _request('post', {'barcode': 'E01099L8', 'library_id': '10',
                                    'restart': 'on'})
        with mock.patch.object(TapeService, 'adopt') as adopt, \
             mock.patch.object(views.units, 'restart_library') as restart:
            adopt.return_value = mock.Mock(success=False,
                                           message='The library has no empty slots')
            views.AdoptTapeView().post(request)
        restart.assert_not_called()
        notes = [str(m) for m in request._messages]
        self.assertTrue(any('no empty slots' in note for note in notes), notes)

    def test_a_missing_barcode_is_refused_before_the_service(self):
        with mock.patch.object(TapeService, 'adopt') as adopt:
            views.AdoptTapeView().post(_request('post', {'library_id': '10'}))
        adopt.assert_not_called()


class InventoryDriftTests(FixtureConfigMixin, TestCase):
    """The robot's inventory against library_contents.

    A tape created after the daemon started is in the file and not in the
    robot; the status page showed the old inventory and said nothing, so a
    tape created minutes earlier looked missing.
    """

    def _state(self, barcodes):
        """An mtx status holding exactly these barcodes in its slots."""
        state = mtx.parse((FIXTURES / 'mtx-status-lib10.txt').read_text())
        for index, slot in enumerate(state.slots):
            slot.barcode = barcodes[index] if index < len(barcodes) else None
        for drive in state.drives:
            drive.barcode = None
        for slot in state.import_export:
            slot.barcode = None
        return state

    def test_no_difference_is_no_warning(self):
        from apps.libraries.services.config.service import ConfigService
        configured = ConfigService(self.config).library_contents(10).barcodes
        self.assertEqual(views._inventory_drift(10, self._state(configured)), {})

    def test_a_tape_the_robot_has_not_seen_yet_is_reported(self):
        from apps.libraries.services.config.service import ConfigService
        configured = ConfigService(self.config).library_contents(10).barcodes
        drift = views._inventory_drift(10, self._state(configured[:-1]))
        self.assertEqual(drift['added'], [configured[-1]])
        self.assertEqual(drift['removed'], [])
        self.assertEqual(drift['configured'], len(configured))

    def test_a_tape_taken_out_of_the_configuration_is_reported(self):
        from apps.libraries.services.config.service import ConfigService
        configured = ConfigService(self.config).library_contents(10).barcodes
        drift = views._inventory_drift(10, self._state(configured + ['E01099L8']))
        self.assertEqual(drift['removed'], ['E01099L8'])

    def test_a_library_with_no_contents_file_says_nothing(self):
        self.assertEqual(views._inventory_drift(99, self._state([])), {})

    def test_the_status_page_carries_the_warning(self):
        from apps.libraries.services.config.service import ConfigService
        configured = ConfigService(self.config).library_contents(10).barcodes
        with mock.patch.object(mapping, 'device_for_library', return_value='/dev/sg4'), \
             mock.patch.object(mtx, 'status', return_value=self._state(configured[:-1])):
            status = views._library_status(10)
        self.assertTrue(status['drift']['added'])


class StatusPagePickerTests(FixtureConfigMixin, TestCase):
    """Changing the library or drive from the page's own dropdown.

    Both status pages are reachable as /library-status/20/ and as
    /library-status/?library_id=20, and the dropdown submits the second form.
    The views read only the URL, so picking a library on the page reloaded it
    with nothing selected.
    """

    def _status_page(self, query=None, path_id=None):
        request = _request('get', query or {})
        with mock.patch.object(views, 'get_live_libraries',
                               return_value=[{'library_id': 10, 'name': 'STK L700'},
                                             {'library_id': 20, 'name': 'SONY LIB-302'}]), \
             mock.patch.object(views, '_library_status',
                               return_value={'success': True, 'library_id': 20,
                                             'storage_slots': [], 'drives': [],
                                             'import_export_slots': [], 'raw_output': '',
                                             'slot_summary': {}, 'drift': {},
                                             'device_path': '/dev/sg4', 'error': None}) as status:
            response = views.LibraryStatusView().get(request, path_id)
        return response, status

    def test_the_dropdown_selects_the_library(self):
        response, status = self._status_page(query={'library_id': '20'})
        self.assertEqual(response.status_code, 200)
        status.assert_called_once_with(20)
        self.assertIn('Library 20 Status', response.content.decode())

    def test_the_url_still_works(self):
        _, status = self._status_page(path_id=20)
        status.assert_called_once_with(20)

    def test_nothing_chosen_shows_only_the_picker(self):
        response, status = self._status_page()
        status.assert_not_called()
        self.assertIn('Select Library', response.content.decode())

    def test_a_nonsense_library_id_is_ignored(self):
        response, status = self._status_page(query={'library_id': 'twenty'})
        status.assert_not_called()
        self.assertEqual(response.status_code, 200)

    def test_the_drive_page_reads_its_dropdown_too(self):
        from apps.libraries.models import Drive, Library, LibraryBrand, LibraryModel
        brand = LibraryBrand.objects.create(name='STK', display_name='STK')
        model = LibraryModel.objects.create(brand=brand, name='L700')
        library = Library.objects.create(library_id=10, brand=brand, model=model,
                                         vendor_identification='STK',
                                         product_identification='L700',
                                         unit_serial_number='XYZZY_A',
                                         channel=0, target=0, lun=0, is_active=True)
        Drive.objects.create(library=library, drive_id=11, channel=0, target=1, lun=0,
                             vendor_identification='IBM',
                             product_identification='ULT3580-TD8',
                             product_revision='1068', unit_serial_number='XYZZY_11',
                             is_active=True)
        request = _request('get', {'drive_id': '11'})
        with mock.patch.object(views, '_drive_status',
                               return_value={'success': True, 'drive_id': 11}) as status:
            response = views.DriveStatusView().get(request, None)
        status.assert_called_once_with(11)
        self.assertEqual(response.status_code, 200)


class PickerCarriedIntoFormsTests(FixtureConfigMixin, TestCase):
    """A form page reached from a library keeps that library chosen.

    The tape inventory and the library page link to these with
    ?library_id=N; the pages ignored it and opened on "-- Select Library --",
    so the operator picked the library again on every one.
    """

    LIBRARIES = [{'library_id': 10, 'vendor': 'STK', 'product': 'L700'},
                 {'library_id': 30, 'vendor': 'STK', 'product': 'L80'}]

    def _page(self, view_name, library_id='30'):
        view = getattr(views, view_name)
        with mock.patch.object(views, 'get_live_libraries', return_value=self.LIBRARIES), \
             mock.patch.object(views, 'get_profile', side_effect=Exception('no profile')):
            response = view().get(_request('get', {'library_id': library_id}))
        return response.content.decode()

    def test_create_tape_keeps_it(self):
        body = self._page('CreateTapeView')
        self.assertIn('value="30" selected', body)
        self.assertNotIn('value="10" selected', body)

    def test_bulk_create_keeps_it(self):
        self.assertIn('value="30" selected', self._page('CreateTapesBulkView'))

    def test_delete_tape_keeps_it(self):
        self.assertIn('value="30" selected', self._page('DeleteTapeView'))

    def test_online_and_offline_keep_it(self):
        self.assertIn('value="30" selected', self._page('LibraryOnlineView'))
        self.assertIn('value="30" selected', self._page('LibraryOfflineView'))

    def test_without_a_library_nothing_is_preselected(self):
        body = self._page('CreateTapeView', library_id='')
        self.assertNotIn('selected', body.split('</select>')[0])


class AddDrivePageTests(FixtureConfigMixin, TestCase):
    """The Add Drive page shows what the drive will be and where it lands.

    It used to offer seven hard-coded models whatever the library was, and ask
    for a slot, a SCSI channel, a target and a LUN - four values the POST
    handler threw away, because the service chooses them.
    """

    def _page(self, library_id='30'):
        with mock.patch.object(views, 'get_live_libraries',
                               return_value=[{'library_id': 10, 'vendor': 'STK',
                                              'product': 'L700'},
                                             {'library_id': 30, 'vendor': 'STK',
                                              'product': 'L80'}]):
            response = views.AddDriveView().get(_request('get', {'library_id': library_id}))
        return response.content.decode()

    def test_it_offers_the_drives_this_library_takes(self):
        body = self._page('30')
        self.assertIn('T10000B', body)          # library 30's own drives
        self.assertNotIn('SDX-900V', body)      # a SONY drive, from another profile

    def test_it_shows_where_the_drive_will_go(self):
        body = self._page('30')
        for label in ('Where it will go', 'Drive slot', 'Drive id', 'SCSI target'):
            self.assertIn(label, body)
        self.assertIn('XYZZY_BD5', body)        # the serial it would be given

    def test_it_no_longer_asks_for_what_it_ignores(self):
        body = self._page('30')
        for field in ('name="slot"', 'name="channel"', 'name="target"', 'name="lun"'):
            self.assertNotIn(field, body)

    def test_without_a_library_it_asks_for_one(self):
        body = self._page('')
        self.assertIn('-- Select Library --', body)

    def test_the_endpoint_answers_with_the_plan(self):
        import json
        request = _request('get')
        response = views.drive_placement_ajax(request, 30)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['plan']['slot'], 5)
        self.assertIn('T10000B', payload['plan']['supported'])

    def test_the_endpoint_needs_a_login(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.session = {}
        self.assertEqual(views.drive_placement_ajax(request, 30).status_code, 401)


class LibraryActivityEndpointTests(FixtureConfigMixin, TestCase):
    """The fragment the library page swaps in for live drive state.

    It reads `vtlcmd <drive> stats` - our MHVTL patch - so it keeps answering
    during a backup, which mt and mtx cannot. vtlcmd is faked here.

    The page is given finished sentences, not counters: what a drive is doing
    is decided by the service, so `mhvtl status activity` says the same.
    """

    def _patched(self, answers):
        from apps.libraries.services.drives import service as drive_service
        drive_service.samples.forget()
        return mock.patch.object(drive_service.vtlcmd, 'stats',
                                 side_effect=lambda drive_id: answers.get(drive_id))

    def _html(self, answers, library_id=10):
        with self._patched(answers):
            response = views.library_activity(_request('get'), library_id)
        return response.content.decode()

    def _json(self, answers, library_id=10):
        with self._patched(answers):
            response = views.library_activity(
                _request('get', {'format': 'json'}), library_id)
        return json.loads(response.content)

    def _stats(self, **fields):
        from apps.libraries.services.operations.vtlcmd import TapeStats
        return TapeStats(**{'barcode': 'E01001L8', 'loaded': True, **fields})

    def test_it_answers_with_a_line_per_drive(self):
        html = self._html({})
        self.assertEqual(html.count('class="drive-live"'), 4)
        for drive_id in (11, 12, 13, 14):
            self.assertIn('id="live-%d"' % drive_id, html)

    def test_the_page_is_handed_words_not_numbers(self):
        """The whole point: no arithmetic in the browser."""
        html = self._html({11: self._stats(written=41943040,
                                           written_media=41943040,
                                           capacity=524288000)})
        self.assertIn('holding E01001L8', html)
        self.assertIn('40.0 MB written', html)
        self.assertIn('8.0% of the tape', html)

    def test_an_empty_drive_says_so(self):
        html = self._html({11: self._stats(barcode=None, loaded=False)})
        self.assertIn('>empty<', html)

    def test_a_silent_daemon_keeps_its_line(self):
        html = self._html({})
        self.assertIn('the daemon did not answer', html)

    def test_json_is_there_for_anything_that_wants_numbers(self):
        payload = self._json({11: self._stats(written=41943040)})
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['total'], 4)
        drive = payload['drives'][0]
        self.assertEqual(drive['drive_id'], 11)
        self.assertEqual(drive['stats']['written'], 41943040)
        self.assertEqual(drive['state'], 'holding')

    def test_it_needs_a_login(self):
        request = RequestFactory().get('/')
        request.session = {}
        self.assertEqual(views.library_activity(request, 10).status_code, 401)

    def test_it_does_not_make_the_page_wait_for_a_second_reading(self):
        """A poll every few seconds brings its own comparison; blocking a
        second on every request would not."""
        from apps.libraries.services.drives import service as drive_service
        with mock.patch.object(drive_service.time, 'sleep') as slept, \
                self._patched({11: self._stats()}):
            views.library_activity(_request('get'), 10)
        slept.assert_not_called()
