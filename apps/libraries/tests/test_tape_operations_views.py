"""The operator views, on services directly.

tape_operations_views.py used to go through adapters/tape_operations_service
and adapters/mhvtl_library_service. These check the shapes its templates and
JavaScript read, now built in the views module, and the bugs found on the
way: the library status page and its endpoint - and the mount page's
library-status-lto endpoint in the adapter - raised AttributeError on a
picker_count mtx never had.

mtx, mt and lsscsi are faked; nothing here touches a device.
"""
import ast
import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from .base import TestCase

from apps.libraries import tape_operations_views as views
from apps.libraries.services.core import success_result
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
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10', 'library_contents.30'):
            shutil.copy(FIXTURES / name, self.config)
        self.status = mtx.parse((FIXTURES / 'mtx-status-lib10.txt').read_text())
        self.media = self.tmpdir()
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
                          'slot_origin': 3,
                          # Which generation it is drawn as, from
                          # services/tapes/palette.py - the page renders this
                          # rather than working it out from the barcode.
                          'generation_token': 'lto-8', 'generation': 'LTO-8'})
        self.assertEqual(len(status['import_export_slots']), 4)

    def test_the_status_names_the_generations_it_is_holding(self):
        """For the legend. Only what this library has, in order, named by the
        service - the page loops over it and decides nothing."""
        a, b = self._patched()
        with a, b:
            status = views._library_status(10)
        self.assertEqual(status['generations_present'],
                         [{'token': 'lto-6', 'label': 'LTO-6'},
                          {'token': 'lto-8', 'label': 'LTO-8'}])

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
        # media.list_media runs `sudo find` - real, unfaked, it passed only on
        # a host with password-free sudo and found nothing anywhere else. The
        # listing is faked from the directory this test made, like every
        # other external command in the suite.
        listing = mock.patch.object(
            media, 'list_media',
            side_effect=lambda base=None: sorted(
                p.name for p in (Path(base) if base else self.media).iterdir() if p.is_dir()))
        listing.start()
        self.addCleanup(listing.stop)

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

    def _adopt_row(self, barcode):
        """The row offering one loose tape a home, and the libraries in it."""
        import re

        with mock.patch.object(views, 'get_live_libraries', return_value=[]), \
             mock.patch.object(media, 'usage_for_all', return_value={}):
            body = views.TapeListView().get(_request()).content.decode()
        table = body.split('Tapes on disk with no library')[-1]
        rows = [chunk for chunk in table.split('<tr>') if barcode in chunk]
        self.assertEqual(len(rows), 1, f'{barcode} is not in the table once')
        return rows[0], re.findall(r'<option value="(\d+)"', rows[0])

    def test_only_the_libraries_that_could_take_it_are_offered(self):
        """Every library on the host was offered for every tape, and the
        service then refused the ones whose drives cannot load the density -
        a choice the form knew would fail. In this fixture library 10 takes
        LTO8, 20 is AIT and 30 is T10000."""
        _row, offered = self._adopt_row('E01099L8')
        self.assertEqual(offered, ['10'])

    def test_a_tape_nothing_can_load_is_told_so_instead(self):
        """An empty dropdown is not an answer: nothing here reads LTO-9, so
        the row says what would have to change."""
        (self.media / 'E01099L9').mkdir()
        row, offered = self._adopt_row('E01099L9')
        self.assertEqual(offered, [])
        self.assertIn('No library on this host has a drive that loads LTO9', row)
        self.assertIn('mhvtl drive add', row)
        self.assertNotIn('<form', row)

    def test_each_choice_names_its_library(self):
        """Two libraries of one model are told apart by what they are."""
        row, _offered = self._adopt_row('E01099L8')
        self.assertIn('Library 10 (STK L700)', row)

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
        # The serial it would be given: library 30's fifth drive is id 35,
        # and a serial comes from the device's own id.
        self.assertIn('80000035', body)

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


#: A drive model, as the catalogue spells them. Used by the guard below.
DRIVE_MODEL = re.compile(r'ULT3580-|T10000|SDX-900|Ultrium')

#: A template or a script setting a value, rather than printing prose.
SETS_A_VALUE = re.compile(r'value\s*=|\|\s*default:|=\s*[\'"]|:\s*[\'"]')


def python_values(source):
    """Every string literal in Python source that is not a docstring.

    Which is exactly "a value this code chose": comments are not in the AST
    at all, and a docstring is text about the code rather than a value in it.

    Read through ast rather than with a regex because the first version of the
    guard below used one - stripping ``\"\"\"...\"\"\"`` pairs - and its
    pairing ran away, swallowing whole files. It passed while
    ``DEFAULT_PRODUCT = 'ULT3580-TD8'`` sat in plain sight.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                docstrings.add(id(node.body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


class AddDriveChoiceTests(FixtureConfigMixin, TestCase):
    """What the web sends the service, and what it must not decide itself.

    Library 30 in the fixture is an STK L80 holding four T10000Bs, and both
    handlers answered an absent vendor and model with ``'IBM'`` and
    ``'ULT3580-TD8'`` - so a drive added to it became an IBM LTO-8 drive in a
    StorageTek library. The service had the rule right all along: match the
    drives that are already there.
    """

    def _added(self, request):
        """The drive_data the service was handed, and the result it returned."""
        seen = {}

        def remember(library_id, drive_data=None, **kwargs):
            seen['library_id'] = library_id
            seen['drive_data'] = dict(drive_data or {})
            return success_result('Drive 35 added', {'drive_id': 35})

        service = mock.Mock()
        service.add.side_effect = remember
        with mock.patch.object(views, 'get_drive_service',
                               return_value=service):
            response = (views.AddDriveView().post(request)
                        if request.method == 'POST'
                        and request.content_type != 'application/json'
                        else views.add_drive_ajax(request))
        return seen, response

    def test_the_form_sends_only_what_was_chosen(self):
        seen, _response = self._added(
            _request('post', {'library_id': '30', 'vendor': '', 'product': ''}))
        self.assertEqual(seen['library_id'], 30)
        self.assertEqual(seen['drive_data'], {},
                         'an empty field is a question for the service, '
                         'not an IBM ULT3580-TD8')

    def test_the_form_passes_a_real_choice_through(self):
        seen, _response = self._added(
            _request('post', {'library_id': '30', 'vendor': 'STK',
                              'product': 'T10000C', 'serial': 'ABC123'}))
        self.assertEqual(seen['drive_data'], {'vendor': 'STK',
                                              'product': 'T10000C',
                                              'serial': 'ABC123'})

    def test_the_endpoint_sends_only_what_was_chosen(self):
        seen, _response = self._added(_request(body={'library_id': 30}))
        self.assertEqual(seen['drive_data'], {},
                         'a scripted call that names no model used to get an '
                         'IBM ULT3580-TD8 whatever the library was')

    def test_the_endpoint_passes_a_real_choice_through(self):
        seen, _response = self._added(
            _request(body={'library_id': 30, 'product': 'T10000C'}))
        self.assertEqual(seen['drive_data'], {'product': 'T10000C'})

    def test_whitespace_is_not_a_choice(self):
        seen, _response = self._added(
            _request('post', {'library_id': '30', 'vendor': '  ',
                              'product': ' T10000C '}))
        self.assertEqual(seen['drive_data'], {'product': 'T10000C'})

    def test_an_stk_library_really_gets_an_stk_drive(self):
        """Through the service, not a mock of it: the page posts what the
        operator left alone, and what lands in device.conf is the model the
        library's own drives are."""
        request = _request('post', {'library_id': '30'})
        views.AddDriveView().post(request)
        stanza = (self.config / 'device.conf').read_text().split(
            'Drive: 35 ')[1].split('Drive:')[0]
        self.assertIn(' Vendor identification: STK\n', stanza)
        self.assertIn(' Product identification: T10000B\n', stanza)

    def test_no_front_end_chooses_a_drive_model_of_its_own(self):
        """The guard, and the reason this unit exists.

        Which model and vendor a new drive gets is the service's answer:
        DriveService._which_drive takes what was asked for, else the model the
        library's other drives are, else the first one its model takes. A
        front end that names a model is a second answer that nothing checks -
        and the second answer was 'IBM' and 'ULT3580-TD8', in two handlers and
        a template.

        The services are not searched. The rule lives there, and
        DEFAULT_PRODUCT is its documented last resort for a library no profile
        knows - tested, rather than hidden in a view.

        Named is not the same as chosen: the iSCSI guide prints a worked
        `lsscsi` listing with an IBM ULT3580-TD8 in it, which is a drive an
        operator reads about. So Python is read through the AST - every string
        literal that is not a docstring, which is exactly "a value in the
        code", and comments are not in the tree at all - and a template or a
        script only where a value is being set.
        """
        STRIPPED = (r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}',
                    r'\{#.*?#\}', r'<!--.*?-->', r'^[ \t]*//.*$', r'/\*.*?\*/')
        MODEL, ASSIGNED = DRIVE_MODEL, SETS_A_VALUE

        app = Path(views.__file__).resolve().parent
        looked_at, chosen = 0, []
        for path in sorted(app.rglob('*')):
            if path.suffix not in ('.py', '.html', '.js') or not path.is_file():
                continue
            parts = path.relative_to(app).parts
            if {'tests', 'services', 'migrations'} & set(parts):
                continue
            looked_at += 1
            if path.suffix == '.py':
                guilty = any(MODEL.search(value)
                             for value in python_values(path.read_text()))
            else:
                text = path.read_text()
                for pattern in STRIPPED:
                    text = re.sub(pattern, '', text, flags=re.S | re.M)
                guilty = any(MODEL.search(line) and ASSIGNED.search(line)
                             for line in text.splitlines())
            if guilty:
                chosen.append(str(path.relative_to(app)))

        self.assertGreater(looked_at, 20, 'nothing was examined')
        self.assertEqual(chosen, [], 'a drive model is chosen outside the '
                                     'services: ' + ', '.join(chosen))

    def test_the_guard_can_see_a_literal_in_a_view(self):
        """The guard above passed while a regex swallowed whole files, so it
        is checked against the bug it exists to catch - the line that was in
        tape_operations_views.py, and the one that was in the template."""
        values = python_values(
            '"""A docstring naming ULT3580-TD8 is fine."""\n'
            '# so is a comment about ULT3580-TD8\n'
            "vendor = request.POST.get('product', 'ULT3580-TD8')\n")
        self.assertEqual([v for v in values if DRIVE_MODEL.search(v)],
                         ['ULT3580-TD8'],
                         'the docstring and the comment should be ignored and '
                         'the argument should not')

        line = """value="{{ plan.vendor|default:'ULT3580-TD8' }}" """
        self.assertTrue(DRIVE_MODEL.search(line) and SETS_A_VALUE.search(line))
        self.assertFalse(SETS_A_VALUE.search(
            '[0:0:1:0]  tape  IBM  ULT3580-TD8  0104  /dev/st0'),
            'a quoted lsscsi listing is prose, not a choice')


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


class EveryFormComesBackToItsLibraryTests(TestCase):
    """A POST that redirects must say which library the next page is about.

    Both inventories load nothing without one - TapeListView and
    DriveListView read `?library_id=` and otherwise fall through to the
    picker - so creating a tape sent the operator back to choosing the
    library they had just put a tape in. Every error path had the same hole,
    where it cost more: the page came back with the library cleared and the
    typing gone.

    One view had it right, AdoptTapeView, which is how it was found.
    """

    def _redirect(self, view, data, patch=None, result=None):
        """POST `data` to `view` with its service faked, and return the URL."""
        answer = result if result is not None else mock.Mock(
            success=True, message='done', data={}, errors=[])
        request = _request('post', data)
        if patch:
            where, what = patch
            with mock.patch.object(where, what, return_value=answer):
                response = view().post(request)
        else:
            response = view().post(request)
        self.assertEqual(response.status_code, 302)
        return response.url

    # -- the one that was reported -----------------------------------------

    def test_creating_a_tape_lands_on_that_library_s_inventory(self):
        url = self._redirect(
            views.CreateTapeView,
            {'library_id': '10', 'barcode': 'E01050L8', 'slot': '5'},
            patch=(TapeService, 'create'))
        self.assertIn('library_id=10', url)
        self.assertIn('/tapes/', url)

    def test_a_refused_tape_comes_back_to_the_form_still_on_that_library(self):
        url = self._redirect(
            views.CreateTapeView,
            {'library_id': '10', 'barcode': 'E01050L8', 'slot': '5'},
            patch=(TapeService, 'create'),
            result=mock.Mock(success=False, message='no', data={}, errors=[]))
        self.assertIn('library_id=10', url)
        self.assertIn('create', url)

    def test_a_half_filled_form_keeps_the_library_too(self):
        """The library is chosen first and the barcode typed after it, so
        this is the path an operator hits most and the one that cost the
        most: it used to clear the picker as well as the field."""
        url = self._redirect(views.CreateTapeView, {'library_id': '10'})
        self.assertIn('library_id=10', url)

    # -- and the rest of them ----------------------------------------------

    def test_a_bulk_create_does_the_same(self):
        url = self._redirect(
            views.CreateTapesBulkView,
            {'library_id': '20', 'count': '4'},
            patch=(TapeService, 'create_bulk'),
            result=mock.Mock(success=True, message='done',
                             data={'created': ['a', 'b', 'c', 'd']},
                             errors=[]))
        self.assertIn('library_id=20', url)

    def test_a_count_out_of_range_keeps_the_library(self):
        url = self._redirect(views.CreateTapesBulkView,
                             {'library_id': '20', 'count': '9999'})
        self.assertIn('library_id=20', url)

    def test_deleting_a_tape_lands_on_that_library_s_inventory(self):
        url = self._redirect(views.DeleteTapeView,
                             {'library_id': '10', 'barcode': 'E01001L8'},
                             patch=(views, '_delete_tape'))
        self.assertIn('library_id=10', url)

    def test_setting_a_library_online_comes_back_to_it(self):
        from apps.libraries.services.operations import OperationsService
        url = self._redirect(views.LibraryOnlineView, {'library_id': '30'},
                             patch=(OperationsService, 'online'))
        self.assertIn('library_id=30', url)

    def test_setting_a_library_offline_comes_back_to_it(self):
        from apps.libraries.services.operations import OperationsService
        url = self._redirect(views.LibraryOfflineView, {'library_id': '30'},
                             patch=(OperationsService, 'offline'))
        self.assertIn('library_id=30', url)

    def test_adding_a_drive_lands_on_that_library_s_drives(self):
        url = self._redirect(views.AddDriveView, {'library_id': '10'},
                             patch=(views.DriveService, 'add'))
        self.assertIn('library_id=10', url)
        self.assertIn('drives', url)

    def test_removing_a_drive_follows_the_service_not_the_form(self):
        """The drive's own library, because the form can be a stale tab and
        the service has just read device.conf."""
        url = self._redirect(
            views.RemoveDriveView, {'drive_id': '11', 'library_id': '20'},
            patch=(views.DriveService, 'remove'),
            result=mock.Mock(success=True, message='done',
                             data={'library_id': 10}, errors=[]))
        self.assertIn('library_id=10', url)

    def test_removing_a_drive_falls_back_to_the_form(self):
        url = self._redirect(
            views.RemoveDriveView, {'drive_id': '11', 'library_id': '20'},
            patch=(views.DriveService, 'remove'),
            result=mock.Mock(success=True, message='done', data={},
                             errors=[]))
        self.assertIn('library_id=20', url)

    # -- what is not put in a URL ------------------------------------------

    def test_a_library_that_is_not_a_number_is_dropped(self):
        """It arrives only from a tampered or stale POST, and the list page
        would answer `?library_id=nonsense` with a second "Invalid library"
        on top of the real complaint."""
        url = self._redirect(views.CreateTapeView, {'library_id': 'nonsense'})
        self.assertNotIn('library_id', url)

    def test_no_library_at_all_is_dropped(self):
        url = self._redirect(views.CreateTapeView, {'barcode': 'E01050L8'})
        self.assertNotIn('library_id', url)

    # -- the guard ---------------------------------------------------------

    def test_no_post_redirects_to_a_bare_route_name(self):
        """The regression this class exists for, checked across the module
        rather than view by view: a `redirect('libraries:...')` with nothing
        after it is a page that will open on the picker.

        The login redirect is exempt - there is no library to carry into it -
        and so is `redirect(back)`, a named URL built with the id in it.
        """
        source = (Path(views.__file__)).read_text()
        offenders = [line.strip() for line in source.splitlines()
                     if re.search(r"redirect\(\s*'libraries:", line)]
        self.assertEqual(offenders, [], 'these lose the library: '
                                        + '; '.join(offenders))

    def test_the_helper_is_what_they_all_use(self):
        """No view builds the URL itself any more.

        Two of them hardcoded the path - `/libraries/operator/move/?...` -
        which survives a renamed route silently, and four more reversed it
        and appended the query string by hand. `_showing` is where that
        expression lives now, and `redirect(back)` in the LTFS helpers is the
        one remaining variable, named because it is used three times.
        """
        source = Path(views.__file__).read_text()
        self.assertGreater(len(re.findall(r'_showing\(', source)), 20)

        built_by_hand = [
            line.strip() for line in source.splitlines()
            if re.search(r'redirect\(\s*f[\'"]', line)
            or re.search(r"redirect\(\s*['\"]/libraries", line)]
        # The one left is inside _showing itself, which is the implementation.
        self.assertEqual(len(built_by_hand), 1, built_by_hand)
        self.assertIn('{target}?library_id=', built_by_hand[0])
