"""sync_mhvtl_to_django(): device.conf into the database.

The tests write a real device.conf into a scratch directory; the function
used to be tested against a mocked adapter, which is how the unreadable-file
case - an empty list, and so every library deactivated - went unnoticed.
"""
import tempfile
from pathlib import Path

from .base import TestCase

from apps.libraries.models import Drive, Library, LibraryBrand, LibraryModel
from apps.libraries.services.sync.service import (forget_library, record_library,
                                                  sync_mhvtl_to_django)


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
        self.config = self.tmpdir()

    def sync(self, text):
        """The counts, for the assertions below.

        The service returns a ServiceResult; its message and its refusal are
        checked in WhatItReportsTests, and this unwraps the data so that the
        tests about *what it changed* stay about that.
        """
        (self.config / 'device.conf').write_text('VERSION: 5\n\n' + text)
        result = sync_mhvtl_to_django(self.config)
        self.assertTrue(result.success, result.message)
        return result.data

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
        refused = sync_mhvtl_to_django(self.config)      # no device.conf at all
        self.assertFalse(refused.success)
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

    def test_what_it_reports(self):
        """It returns a ServiceResult, which is this layer's rule and was
        the one thing this module did not do.

        It raised ConfigUnreadable and returned a bare dict, so each of the
        nine callers invented its own handling: the web caught it and said
        "The database was not updated", the AJAX endpoints returned a JSON
        error, and `mhvtl config sync` caught nothing at all - an unreadable
        device.conf came out as a Python traceback under "this is a bug".
        """
        done = sync_mhvtl_to_django(self.config)        # no device.conf
        self.assertFalse(done.success)
        self.assertIn('could not be read', done.message)
        self.assertIn('the database was left as it is', done.message)
        self.assertTrue(any(str(self.config) in detail
                            for detail in done.errors),
                        f'the refusal should name where it looked: {done.errors}')

    def test_the_sentence_names_what_changed(self):
        """Composed once here. Three callers composed their own from the
        counts and two of them said nearly the same thing in different
        words."""
        (self.config / 'device.conf').write_text(
            'VERSION: 5\n\n' + library(10) + drive(11, 10, 1, 1))
        said = sync_mhvtl_to_django(self.config).message
        self.assertIn('1 library in device.conf', said)
        self.assertIn('1 new', said)
        self.assertIn('1 drive(s) imported', said)

    def test_the_sentence_says_so_when_nothing_changed(self):
        """Rather than listing eight zeros, which is what the page did."""
        self.sync(library(10))
        said = sync_mhvtl_to_django(self.config).message
        self.assertEqual(said, '1 library in device.conf, nothing to change')

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


class RecordOneLibraryTest(TestCase):
    """record_library() and forget_library(): one library, never the others.

    The scope is the point. sync_mhvtl_to_django() deactivates every row
    device.conf does not mention, and the parser does not raise on a damaged
    file - it returns a shorter library list - so a whole-configuration
    reconcile is the wrong thing to run as a step of "I created one library".
    """

    def setUp(self):
        self.config = self.tmpdir()

    def write(self, text):
        (self.config / 'device.conf').write_text('VERSION: 5\n\n' + text)

    def existing(self, library_id, active=True, vendor='STK', product='L700'):
        brand, _ = LibraryBrand.objects.get_or_create(
            name=vendor, defaults={'display_name': vendor})
        model, _ = LibraryModel.objects.get_or_create(brand=brand, name=product)
        return Library.objects.create(
            library_id=library_id, channel=0, target=0, lun=0, brand=brand,
            model=model, vendor_identification=vendor,
            product_identification=product, is_active=active)

    def test_creates_the_row_and_its_drives(self):
        self.write(library(60) + drive(61, 60, 1, 1) + drive(62, 60, 2, 2))
        recorded = record_library(60, self.config)
        self.assertTrue(recorded['ok'], recorded['message'])
        self.assertTrue(recorded['created'])
        self.assertEqual(recorded['drives'], 2)
        self.assertEqual(Library.objects.get(library_id=60).is_active, True)
        self.assertEqual(Drive.objects.filter(library__library_id=60).count(), 2)

    def test_activates_a_row_left_inactive(self):
        """Library 60 in device.conf, running, and invisible in every dropdown:
        get_live_libraries() drops a configured library whose row is inactive."""
        self.existing(60, active=False)
        self.write(library(60) + drive(61, 60, 1, 1))
        recorded = record_library(60, self.config)
        self.assertTrue(recorded['ok'])
        self.assertFalse(recorded['created'])
        self.assertTrue(Library.objects.get(library_id=60).is_active)

    def test_leaves_every_other_library_alone(self):
        """The whole reason this is scoped: library 20 is not in this file."""
        self.existing(20, active=True)
        self.write(library(60))
        record_library(60, self.config)
        self.assertTrue(Library.objects.get(library_id=20).is_active)

    def test_a_truncated_file_cannot_deactivate_anything(self):
        """A truncated device.conf parses to fewer libraries, not to an error:
        the cut lost library 60 entirely, and nothing else may be touched."""
        self.existing(20, active=True)
        self.existing(30, active=True)
        self.write(library(20) + 'Library: 60 CHANNEL: 00 TAR')
        recorded = record_library(60, self.config)
        self.assertFalse(recorded['ok'])
        self.assertIn('does not declare library 60', recorded['message'])
        self.assertTrue(Library.objects.get(library_id=20).is_active)
        self.assertTrue(Library.objects.get(library_id=30).is_active)

    def test_a_garbage_file_cannot_deactivate_anything(self):
        """It parses to zero libraries without raising, which is what makes a
        whole-configuration reconcile dangerous here."""
        self.existing(20, active=True)
        (self.config / 'device.conf').write_text('not a device.conf at all\n')
        recorded = record_library(60, self.config)
        self.assertFalse(recorded['ok'])
        self.assertTrue(Library.objects.get(library_id=20).is_active)

    def test_a_stanza_without_a_home_directory_still_records(self):
        """The column is NOT NULL with a default, and passing None overrides the
        default rather than falling back to it."""
        self.write('Library: 60 CHANNEL: 00 TARGET: 00 LUN: 00\n'
                   ' Vendor identification: STK\n'
                   ' Product identification: L700\n\n')
        recorded = record_library(60, self.config)
        self.assertTrue(recorded['ok'], recorded['message'])
        self.assertTrue(Library.objects.get(library_id=60).home_directory)

    def test_an_unreadable_file_changes_nothing(self):
        self.existing(20, active=True)
        recorded = record_library(60, self.config)      # no device.conf written
        self.assertFalse(recorded['ok'])
        self.assertIn('could not be read', recorded['message'])
        self.assertTrue(Library.objects.get(library_id=20).is_active)

    def test_a_drive_no_longer_declared_is_deactivated_within_the_library(self):
        self.write(library(60) + drive(61, 60, 1, 1) + drive(62, 60, 2, 2))
        record_library(60, self.config)
        self.write(library(60) + drive(61, 60, 1, 1))
        recorded = record_library(60, self.config)
        self.assertEqual(recorded['drives'], 1)
        self.assertFalse(Drive.objects.get(drive_id=62).is_active)

    def test_forget_deactivates_the_row_and_its_drives(self):
        self.write(library(60) + drive(61, 60, 1, 1))
        record_library(60, self.config)
        forgotten = forget_library(60)
        self.assertTrue(forgotten['ok'])
        self.assertFalse(Library.objects.get(library_id=60).is_active)
        self.assertFalse(Drive.objects.get(drive_id=61).is_active)

    def test_forget_keeps_the_row(self):
        """Deleting rows is the cleanup page's decision, and an inactive row is
        what lets a recreated id be recognised rather than duplicated."""
        self.existing(60)
        forget_library(60)
        self.assertTrue(Library.objects.filter(library_id=60).exists())

    def test_forget_an_unknown_library_is_not_an_error(self):
        forgotten = forget_library(999)
        self.assertTrue(forgotten['ok'])
        self.assertIn('No database row', forgotten['message'])
