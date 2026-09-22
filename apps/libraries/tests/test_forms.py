# apps/libraries/tests/test_forms.py
from django.test import TestCase
from apps.libraries.forms import LibraryConfigForm
from apps.libraries.models import LibraryBrand, LibraryModel


class LibraryFormTests(TestCase):
    def setUp(self):
        self.brand = LibraryBrand.objects.create(name='HP', display_name='HP')
        self.model = LibraryModel.objects.create(brand=self.brand, name='MSL G3')

    def test_library_config_form_valid(self):
        """Test valid library configuration form"""
        form_data = {
            'library_id': 10,
            'channel': 0,
            'target': 0,
            'lun': 0,
            'model_id': self.model.pk,
            'product_revision': '1068',
            'media_count': 10,
            'empty_slots': 5,
            'home_directory': '/opt/mhvtl',
        }
        form = LibraryConfigForm(brand=self.brand, data=form_data)
        self.assertTrue(form.is_valid(), msg=form.errors)

    def test_slot_limit_validation(self):
        """Test 15,000 slot limit validation"""
        form_data = {
            'library_id': 10,
            'channel': 0,
            'target': 0,
            'lun': 0,
            'model_id': self.model.pk,
            'product_revision': '1068',
            'media_count': 10000,
            'empty_slots': 6000,  # Total: 16,000 - should fail
            'home_directory': '/opt/mhvtl',
        }
        form = LibraryConfigForm(brand=self.brand, data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn('cannot exceed 15000', str(form.errors))
