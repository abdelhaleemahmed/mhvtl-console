from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.libraries.models import Library, LibraryBrand, LibraryModel

User = get_user_model()

class BasicLibraryTest(TestCase):
    """Basic test to verify testing framework works"""

    def setUp(self):
        """Set up test data"""
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )

    def test_models_exist(self):
        """Test that models can be imported"""
        self.assertIsNotNone(Library)
        self.assertIsNotNone(LibraryBrand)
        self.assertIsNotNone(LibraryModel)

    def test_dashboard_requires_login(self):
        """Test that dashboard requires authentication"""
        response = self.client.get('/libraries/')
        # Should redirect to login
        self.assertEqual(response.status_code, 302)

    def test_dashboard_with_login(self):
        """Test dashboard access with logged in user"""
        self.client.login(username='testuser', password='testpass123')
        response = self.client.get('/libraries/')
        # May redirect depending on whether libraries exist
        self.assertIn(response.status_code, [200, 302])
