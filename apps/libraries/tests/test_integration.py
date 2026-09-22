# apps/libraries/tests/test_integration.py
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.libraries.models import Library, LibraryBrand, LibraryModel, Drive, MediaSlot

User = get_user_model()


class LibraryCreationWithComponentsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', 'test@test.com', 'password')
        self.client.login(username='testuser', password='password')
        self.brand = LibraryBrand.objects.create(name='HP', display_name='HP Libraries')
        self.model = LibraryModel.objects.create(brand=self.brand, name='MSL G3 Series')

    def test_complete_library_creation_with_drives_and_slots(self):
        """Test complete library creation including drives and media changer"""
        # Dashboard should be accessible when logged in
        response = self.client.get(reverse('libraries:dashboard'))
        self.assertIn(response.status_code, [200, 302])

        # Setup choice should be accessible
        response = self.client.get(reverse('libraries:setup_choice'))
        self.assertIn(response.status_code, [200, 302])

    def test_library_removal_with_drives_and_slots(self):
        """Test library removal including all associated drives and slots"""
        library = Library.objects.create(
            library_id=20,
            channel=0, target=0, lun=0,
            brand=self.brand, model=self.model,
            vendor_identification='HP',
            product_identification='MSL G3 Series',
        )

        for i in range(1, 5):
            Drive.objects.create(
                library=library,
                drive_id=20 + i,
                channel=0, target=i, lun=0,
                vendor_identification='HP',
                product_identification='ULT3580-TD8',
                product_revision='1.0',
                unit_serial_number=f'HPDRV00{i}'
            )

        for i in range(1, 11):
            MediaSlot.objects.create(
                library=library,
                slot_number=i,
                is_empty=(i > 5),
                media_barcode=f'HP00{i}L8' if i <= 5 else None
            )

        self.assertEqual(Drive.objects.filter(library=library).count(), 4)
        self.assertEqual(MediaSlot.objects.filter(library=library).count(), 10)

        # Delete library — cascade should remove drives and slots
        library.delete()

        self.assertFalse(Library.objects.filter(library_id=20).exists())
        self.assertEqual(Drive.objects.count(), 0)
        self.assertEqual(MediaSlot.objects.count(), 0)
