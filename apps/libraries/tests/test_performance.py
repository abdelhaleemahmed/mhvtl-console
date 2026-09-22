# apps/libraries/tests/test_performance.py
from django.test import TestCase, Client
from django.test.utils import override_settings
from apps.libraries.models import Library, LibraryBrand, LibraryModel
import time


class PerformanceTests(TestCase):
    def test_dashboard_load_time(self):
        """Dashboard should load in under 2 seconds"""
        client = Client()
        start = time.time()
        response = client.get('/libraries/')
        end = time.time()
        self.assertLess(end - start, 2.0)

    def test_library_list_with_many_items(self):
        """Test performance with 100+ libraries"""
        brand = LibraryBrand.objects.create(name='STK', display_name='StorageTek')
        model = LibraryModel.objects.create(brand=brand, name='L700')
        for i in range(100):
            Library.objects.create(
                library_id=i + 1,
                channel=0, target=0, lun=0,
                brand=brand,
                model=model,
                vendor_identification='STK',
                product_identification='L700',
            )

        start = time.time()
        response = self.client.get('/libraries/list/')
        end = time.time()
        self.assertLess(end - start, 3.0)
