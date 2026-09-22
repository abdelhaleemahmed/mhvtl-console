# apps/libraries/tests/test_removal.py
from django.test import TestCase
from apps.libraries.models import Library, Drive, MediaSlot, LibraryBrand, LibraryModel


class LibraryRemovalTests(TestCase):
    def setUp(self):
        self.brand = LibraryBrand.objects.create(name='STK', display_name='STK')
        self.model = LibraryModel.objects.create(brand=self.brand, name='L700')

    def test_cascade_deletion(self):
        """Test that removing library removes all associated components"""
        library = Library.objects.create(
            library_id=70,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='STK',
            product_identification='L700',
        )

        for i in range(3):
            Drive.objects.create(
                library=library,
                drive_id=70 + i + 1,
                channel=0, target=i + 1, lun=0,
                vendor_identification='STK',
                product_identification='ULTRIUM-8',
                product_revision='1.0',
                unit_serial_number=f'DRV{i + 1:05d}',
            )

        for i in range(50):
            MediaSlot.objects.create(
                library=library,
                slot_number=i + 1
            )

        self.assertEqual(Drive.objects.filter(library=library).count(), 3)
        self.assertEqual(MediaSlot.objects.filter(library=library).count(), 50)

        library.delete()

        self.assertEqual(Drive.objects.count(), 0)
        self.assertEqual(MediaSlot.objects.count(), 0)

    def test_removal_with_media_preservation(self):
        """Test library removal preserves media barcode data before deletion"""
        library = Library.objects.create(
            library_id=80,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='STK',
            product_identification='L700',
        )

        media_barcodes = []
        for i in range(10):
            barcode = f'PRESERVE{i:03d}'
            media_barcodes.append(barcode)
            MediaSlot.objects.create(
                library=library,
                slot_number=i + 1,
                media_barcode=barcode,
                is_empty=False
            )

        # Verify barcodes exist before deletion
        slots = MediaSlot.objects.filter(library=library, is_empty=False)
        self.assertEqual(slots.count(), 10)
        saved_barcodes = list(slots.values_list('media_barcode', flat=True))
        self.assertEqual(sorted(saved_barcodes), sorted(media_barcodes))
