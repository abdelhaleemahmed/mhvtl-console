"""sync_mhvtl_to_django(): device.conf into the database.

The tests write a real device.conf into a scratch directory; the function
used to be tested against a mocked adapter, which is how the unreadable-file
case - an empty list, and so every library deactivated - went unnoticed.
"""
import tempfile
from pathlib import Path

from django.test import TestCase

from apps.libraries.models import Drive, Library, LibraryBrand, LibraryModel
from apps.libraries.services.sync.service import ConfigUnreadable, sync_mhvtl_to_django


def library(library_id, vendor='STK', product='L700', target=0):
    return (f'Library: {library_id} CHANNEL: 00 TARGET: {target:02d} LUN: 00\n'
            f' Vendor identification: {vendor}\n'
            f' Product identification: {product}\n'
            f' Unit serial number: SN{library_id:05d}\n'
            f' NAA: {library_id}:22:33:44:ab:00:{target:02d}:00\n'
            f' Home directory: /opt/mhvtl\n\n')


def drive(drive_id, library_id, slot, target, product='ULT3580-TD8'):
    return (f'Drive: {drive_id} CHANNEL: 00 TARGET: {target:02d} LUN: 00\n'
            f' Library ID: {library_id} Slot: {slot:02d}\n'
            f' Vendor identification: IBM\n'
            f' Product identification: {product}\n'
            f' Unit serial number: DRV{drive_id:05d}\n\n')


class SyncServiceTest(TestCase):

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())

    def sync(self, text):
        (self.config / 'device.conf').write_text('VERSION: 5\n\n' + text)
        return sync_mhvtl_to_django(self.config)

    def existing(self, library_id, active=True):
        brand, _ = LibraryBrand.objects.get_or_create(name='STK',
                                                      defaults={'display_name': 'STK'})
        model, _ = LibraryModel.objects.get_or_create(brand=brand, name='L700')
        return Library.objects.create(
            library_id=library_id, channel=0, target=0, lun=0, brand=brand,
            model=model, vendor_identification='STK',
            product_identification='L700', is_active=active)

    def test_creates_new_libraries(self):
        stats = self.sync(library(10) + library(20, 'IBM', 'TS3500', target=5))
        self.assertEqual((stats['libraries_found'], stats['created'], stats['total_db']),
                         (2, 2, 2))
        created = Library.objects.get(library_id=20)
        self.assertEqual(created.vendor_identification, 'IBM')
        self.assertEqual(created.target, 5)
        self.assertEqual(created.unit_serial_number, 'SN00020')

    def test_imports_drives_for_new_libraries(self):
        stats = self.sync(library(10) + drive(11, 10, 1, 1) + drive(12, 10, 2, 2)
                          + drive(13, 10, 3, 3))
        self.assertEqual(stats['drives_imported'], 3)
        drives = Drive.objects.filter(library__library_id=10).order_by('drive_id')
        self.assertEqual([d.drive_id for d in drives], [11, 12, 13])
        self.assertEqual(drives[1].target, 2)
        self.assertEqual(drives[0].product_identification, 'ULT3580-TD8')

    def test_skips_existing_libraries(self):
        self.existing(10)
        stats = self.sync(library(10))
        self.assertEqual(stats['created'], 0)
        self.assertEqual(Library.objects.count(), 1)

    def test_activates_inactive_libraries(self):
        self.existing(10, active=False)
        stats = self.sync(library(10))
        self.assertEqual(stats['activated'], 1)
        self.assertTrue(Library.objects.get(library_id=10).is_active)

    def test_deactivates_libraries_device_conf_no_longer_has(self):
        self.existing(99)
        stats = self.sync(library(10))
        self.assertEqual(stats['deactivated'], 1)
        self.assertFalse(Library.objects.get(library_id=99).is_active)

    def test_an_unreadable_device_conf_changes_nothing(self):
        """It used to read as "no libraries" and deactivate every one."""
        self.existing(10)
        with self.assertRaises(ConfigUnreadable):
            sync_mhvtl_to_django(self.config)            # no device.conf at all
        self.assertTrue(Library.objects.get(library_id=10).is_active)

    def test_no_changes_when_in_sync(self):
        self.existing(10)
        stats = self.sync(library(10))
        self.assertEqual((stats['created'], stats['activated'], stats['deactivated']),
                         (0, 0, 0))

    def test_creates_brand_and_model_if_missing(self):
        self.sync(library(10, 'NEWVENDOR', 'NEWMODEL'))
        self.assertTrue(LibraryBrand.objects.filter(name='NEWVENDOR').exists())
        self.assertTrue(LibraryModel.objects.filter(name='NEWMODEL').exists())

    def test_return_value_structure(self):
        stats = self.sync('')
        self.assertEqual(set(stats), {'libraries_found', 'created', 'updated',
                                      'drives_imported',
                                      'drives_activated', 'drives_deactivated',
                                      'activated', 'deactivated', 'total_db',
                                      'total_drives'})

    def test_a_row_that_no_longer_matches_device_conf_is_corrected(self):
        """A library deleted and recreated keeps its id: the row described the
        library that used to have it - wrong vendor, product, brand and SCSI
        address on every page that reads the database."""
        stale = self.existing(10)                        # STK L700 in setUp
        stats = self.sync(library(10, 'IBM', '03584L32', target=7))
        stale.refresh_from_db()
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(stale.vendor_identification, 'IBM')
        self.assertEqual(stale.product_identification, '03584L32')
        self.assertEqual(stale.target, 7)
        self.assertEqual(stale.brand.name, 'IBM')
        self.assertEqual(stale.model.name, '03584L32')

    def test_a_row_that_matches_is_left_alone(self):
        """Sync twice: the second pass has nothing to correct."""
        text = library(10)
        self.sync(text)
        self.assertEqual(self.sync(text)['updated'], 0)

    def add_drive(self, db_lib, drive_id, active):
        return Drive.objects.create(library=db_lib, drive_id=drive_id, channel=0,
                                    target=drive_id, lun=0, is_active=active)

    def test_drives_of_a_known_library_are_reactivated(self):
        """What the host had: libraries active again, every drive inactive,
        and the drive pages empty."""
        db_lib = self.existing(10)
        self.add_drive(db_lib, 11, active=False)
        self.add_drive(db_lib, 12, active=False)
        stats = self.sync(library(10) + drive(11, 10, 1, 1) + drive(12, 10, 2, 2))
        self.assertEqual(stats['drives_activated'], 2)
        self.assertEqual(Drive.objects.filter(is_active=True).count(), 2)

    def test_a_drive_added_to_a_known_library_is_imported(self):
        db_lib = self.existing(10)
        self.add_drive(db_lib, 11, active=True)
        stats = self.sync(library(10) + drive(11, 10, 1, 1) + drive(12, 10, 2, 2))
        self.assertEqual(stats['drives_imported'], 1)
        self.assertTrue(Drive.objects.get(drive_id=12).is_active)

    def test_a_drive_device_conf_dropped_is_deactivated(self):
        db_lib = self.existing(10)
        self.add_drive(db_lib, 11, active=True)
        self.add_drive(db_lib, 12, active=True)
        stats = self.sync(library(10) + drive(11, 10, 1, 1))
        self.assertEqual(stats['drives_deactivated'], 1)
        self.assertFalse(Drive.objects.get(drive_id=12).is_active)

    def test_drives_of_a_removed_library_are_deactivated(self):
        db_lib = self.existing(99)
        self.add_drive(db_lib, 91, active=True)
        self.sync(library(10))
        self.assertFalse(Drive.objects.get(drive_id=91).is_active)
