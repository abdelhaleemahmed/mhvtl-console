# apps/libraries/tests/test_drives.py
from django.test import TestCase
from apps.libraries.models import Library, Drive, LibraryBrand, LibraryModel


class DriveCreationTests(TestCase):
    def setUp(self):
        self.brand = LibraryBrand.objects.create(name='STK', display_name='StorageTek')
        self.model = LibraryModel.objects.create(brand=self.brand, name='SL8500')

    def test_drives_created_with_library(self):
        """Test that drives can be created and associated with a library"""
        library = Library.objects.create(
            library_id=30,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='STK',
            product_identification='SL8500',
            media_count=100, empty_slots=20
        )

        for i in range(8):
            Drive.objects.create(
                library=library,
                drive_id=30 + i + 1,
                channel=0, target=i + 1, lun=0,
                vendor_identification='STK',
                product_identification='ULTRIUM-8',
                product_revision='1.0',
                unit_serial_number=f'STK{i + 1:05d}',
            )

        drives = Drive.objects.filter(library=library)
        self.assertEqual(drives.count(), 8)

        for drive in drives:
            self.assertEqual(drive.vendor_identification, 'STK')
            self.assertIsNotNone(drive.unit_serial_number)
            self.assertTrue(drive.is_active)

    def test_drive_scsi_addressing(self):
        """Test SCSI addressing is correctly assigned to drives"""
        library = Library.objects.create(
            library_id=40,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='STK',
            product_identification='SL8500',
        )

        for i in range(4):
            Drive.objects.create(
                library=library,
                drive_id=40 + i + 1,
                channel=0,
                target=i + 1,
                lun=0,
                vendor_identification='STK',
                product_identification='ULTRIUM-8',
                product_revision='1.0',
                unit_serial_number=f'DRV{i + 1:05d}',
            )

        drives = Drive.objects.filter(library=library)
        targets = [d.target for d in drives]
        self.assertEqual(len(targets), len(set(targets)))  # All unique
