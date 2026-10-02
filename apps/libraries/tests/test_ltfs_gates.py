"""The gates that decide which cartridges get read at all.

Reading whether a cartridge is an LTFS volume means reading the cartridge. Two
questions answer themselves first, and neither touches one:

    1. does any drive in this library open LTFS?   device.conf and a table
    2. can this generation of media be partitioned?  the barcode

What a gate skips must be reported as NOT_ASKED and never as PLAIN. A cartridge
nobody read may well be an LTFS volume written on another system; a library whose
drives cannot mount it does not make it any less of one.
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.tapes import TapeService, ltfs_state, media

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
CONFIG_FILES = [p.name for p in FIXTURES.iterdir() if p.is_file()]


class GenerationGateTests(TestCase):
    """Gate 2: LTO-5 was the first partitionable generation."""

    def test_lto4_and_older_can_never_hold_ltfs(self):
        for barcode in ('ABC001L1', 'ABC001L2', 'ABC001L3', 'ABC001L4'):
            with self.subTest(barcode=barcode):
                self.assertFalse(ltfs_state.could_hold_ltfs(barcode))

    def test_lto5_and_newer_can(self):
        for barcode in ('ABC001L5', 'ABC001L6', 'ABC001L7', 'ABC001L8',
                        'ABC001L9', 'ABC001LA'):
            with self.subTest(barcode=barcode):
                self.assertTrue(ltfs_state.could_hold_ltfs(barcode))

    def test_a_barcode_with_no_lto_generation_is_still_read(self):
        """The 3592 trap: not LTO does not mean not partitionable.

        IBM 3592 media is partitionable and LTFS supports TS1140 and later, so
        treating an unrecognised barcode as impossible would report a 3592 LTFS
        volume as a plain cartridge.
        """
        for barcode in ('JA1234JA', 'T10000A', 'G03001TA', ''):
            with self.subTest(barcode=barcode):
                self.assertTrue(ltfs_state.could_hold_ltfs(barcode))

    def test_an_old_cartridge_is_reported_not_asked_not_plain(self):
        with mock.patch.object(media, 'read_media_files',
                               return_value={}) as reader:
            found = ltfs_state.state_for_all(['ABC001L4'], {'ABC001L4': 1})
        reader.assert_not_called()
        self.assertEqual(found['ABC001L4'].state, ltfs_state.NOT_ASKED)
        self.assertNotEqual(found['ABC001L4'].state, ltfs_state.PLAIN)
        self.assertEqual(found['ABC001L4'].summary, '')

    def test_old_and_new_in_one_call_read_only_the_new(self):
        with mock.patch.object(media, 'read_media_files',
                               return_value={}) as reader:
            ltfs_state.state_for_all(['ABC001L4', 'ABC001L8'], {})
        self.assertEqual(reader.call_args.args[0], ['ABC001L8'])


class LibraryGateTests(TestCase):
    """Gate 1: a library with no LTFS-capable drive reads nothing."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        for name in CONFIG_FILES:
            shutil.copy(FIXTURES / name, self.config)
        self.service = TapeService(self.config)

    def set_drive_vendor(self, vendor):
        path = self.config / 'device.conf'
        path.write_text(path.read_text().replace(
            ' Vendor identification: IBM\n', f' Vendor identification: {vendor}\n'))

    def reads(self, **kwargs):
        with mock.patch.object(media, 'read_media_files',
                              wraps=media.read_media_files) as reader:
            with mock.patch.object(ltfs_state, 'media', media):
                self.service.list(10, **kwargs)
            return reader.call_args_list

    def test_no_ltfs_capable_drive_means_no_cartridge_is_read_for_ltfs(self):
        """Library 30 on this host: STK T10000B, and LTFS has no STK table."""
        self.set_drive_vendor('STK')
        self.assertFalse(self.service._ltfs_possible_here(10))

    def test_a_capable_drive_means_it_is_possible(self):
        self.assertTrue(self.service._ltfs_possible_here(10))

    def test_an_unreadable_device_conf_does_not_filter(self):
        """Cannot tell is not the same as no: a caller that asked gets the read."""
        service = TapeService(self.config / 'does-not-exist')
        self.assertTrue(service._ltfs_possible_here(10))

    def test_the_default_listing_asks_for_no_ltfs_state(self):
        result = self.service.list(10)
        self.assertTrue(all(not t['ltfs_state'] for t in result.data['tapes']))

    def test_asking_for_ltfs_populates_it(self):
        result = self.service.list(10, with_ltfs=True)
        self.assertTrue(any(t['ltfs_state'] for t in result.data['tapes']))

    def test_asking_for_ltfs_on_an_incapable_library_populates_nothing(self):
        self.set_drive_vendor('STK')
        result = self.service.list(10, with_ltfs=True)
        self.assertTrue(all(not t['ltfs_state'] for t in result.data['tapes']))
        self.assertTrue(all(t['ltfs_summary'] == ''
                            for t in result.data['tapes']))

    def test_with_ltfs_turns_usage_on_rather_than_answering_nothing(self):
        """The partition count comes from the usage pass, so asking for LTFS
        without usage would silently return no state at all."""
        result = self.service.list(10, with_usage=False, with_ltfs=True)
        self.assertTrue(any(t['ltfs_state'] for t in result.data['tapes']))


class MountMapLtfsTests(TestCase):
    """mount_status(with_ltfs=True): the map says which cartridges are volumes.

    Off by default, because LTFS is an extra and the tape path must not pay for
    it, and gated the same way TapeService.list() is - a library with no
    LTFS-capable drive reads no cartridge at all.
    """

    DRIVES = [{'drive_num': 0, 'vendor': 'IBM', 'model': 'ULT3580-TD7',
               'revision': 'D.02'}]

    def status(self, *, with_ltfs, drives=None, slots=None, states=None):
        from apps.libraries.services.operations import mounting

        drives = drives if drives is not None else self.DRIVES
        slots = slots if slots is not None else [
            {'slot_num': 1, 'barcode': 'I60001L7', 'full': True},
            {'slot_num': 2, 'barcode': 'I60005L7', 'full': True},
        ]
        element = lambda **kw: mock.Mock(**kw)   # noqa: E731 - test shorthand
        state = mock.Mock(
            drives=[element(number=d['drive_num'], barcode=None, full=False,
                            slot_origin=None) for d in drives],
            slots=[element(number=s['slot_num'], barcode=s['barcode'],
                           full=s['full']) for s in slots],
            import_export=[], map_slots=[])
        configured = [{**d, 'lto_generation': 'LTO-7'} for d in drives]

        with mock.patch.object(mounting.mapping, 'device_for_library',
                               return_value='/dev/sg24'), \
             mock.patch.object(mounting.mtx, 'status', return_value=state), \
             mock.patch.object(mounting, 'library_drives',
                               return_value=configured), \
             mock.patch('apps.libraries.services.tapes.media.usage_for_all',
                        return_value={}), \
             mock.patch('apps.libraries.services.tapes.ltfs_state.state_for_all',
                        return_value=states or {}) as reader:
            result = mounting.mount_status(60, with_ltfs=with_ltfs)
        return result, reader

    @staticmethod
    def volume(state='ltfs', was=False):
        return mock.Mock(state=state, was_ltfs=was, summary='a summary')

    def test_off_by_default_nothing_is_read_and_nothing_is_claimed(self):
        result, reader = self.status(with_ltfs=False)
        reader.assert_not_called()
        for slot in result.data['storage_slots']:
            self.assertNotIn('ltfs', slot)

    def test_a_volume_is_marked_and_a_plain_cartridge_is_not(self):
        result, _ = self.status(
            with_ltfs=True,
            states={'I60001L7': self.volume(),
                    'I60005L7': self.volume(state='plain')})
        marks = {s['barcode']: s['ltfs'] for s in result.data['storage_slots']}
        self.assertEqual(marks, {'I60001L7': True, 'I60005L7': False})

    def test_a_wiped_cartridge_is_marked_was_ltfs(self):
        result, _ = self.status(
            with_ltfs=True,
            states={'I60001L7': self.volume(state='plain', was=True)})
        slot = result.data['storage_slots'][0]
        self.assertFalse(slot['ltfs'])
        self.assertTrue(slot['ltfs_was'])

    def test_every_drive_says_whether_ltfs_opens_it(self):
        result, _ = self.status(with_ltfs=True)
        self.assertTrue(result.data['drives'][0]['ltfs_capable'])

    def test_no_capable_drive_reads_no_cartridge_and_claims_nothing(self):
        """Library 30: 40 cartridges, four STK T10000 drives. Reading all 40 to
        colour tiles nobody can mount is what the gate prevents - and the slots
        report None, because "we did not look" is not "it is not one"."""
        result, reader = self.status(
            with_ltfs=True,
            drives=[{'drive_num': 0, 'vendor': 'STK', 'model': 'T10000B',
                     'revision': ''}])
        reader.assert_not_called()
        self.assertFalse(result.data['drives'][0]['ltfs_capable'])
        for slot in result.data['storage_slots']:
            self.assertIsNone(slot['ltfs'])

    def test_an_incapable_drive_carries_the_reason(self):
        result, _ = self.status(
            with_ltfs=True,
            drives=[{'drive_num': 0, 'vendor': 'STK', 'model': 'T10000B',
                     'revision': ''}])
        self.assertIn('STK', result.data['drives'][0]['ltfs_reason'])
