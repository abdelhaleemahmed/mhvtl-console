# apps/libraries/tests/test_media_changer.py
from django.test import TestCase
from apps.libraries.models import Library, MediaSlot, LibraryBrand, LibraryModel


class MediaChangerTests(TestCase):
    def setUp(self):
        self.brand = LibraryBrand.objects.create(name='STK', display_name='STK')
        self.model = LibraryModel.objects.create(brand=self.brand, name='L700')

    def test_media_slots_creation(self):
        """Test media slots are created correctly"""
        library = Library.objects.create(
            library_id=50,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='STK',
            product_identification='L700',
            media_count=20, empty_slots=10
        )

        for i in range(1, 31):  # 30 total slots
            MediaSlot.objects.create(
                library=library,
                slot_number=i,
                is_empty=(i > 20),
                media_barcode=f'MED{i:04d}L8' if i <= 20 else None,
                media_type='LTO8' if i <= 20 else None
            )

        all_slots = MediaSlot.objects.filter(library=library)
        self.assertEqual(all_slots.count(), 30)

        filled_slots = all_slots.filter(is_empty=False)
        self.assertEqual(filled_slots.count(), 20)

        empty_slots = all_slots.filter(is_empty=True)
        self.assertEqual(empty_slots.count(), 10)

    def test_import_export_slots(self):
        """Test I/E (Import/Export) slots configuration"""
        library = Library.objects.create(
            library_id=60,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='STK',
            product_identification='L700',
        )

        for i in range(1, 101):
            MediaSlot.objects.create(
                library=library,
                slot_number=i,
                is_import_export=(i > 95)
            )

        ie_slots = MediaSlot.objects.filter(
            library=library,
            is_import_export=True
        )
        self.assertEqual(ie_slots.count(), 5)
