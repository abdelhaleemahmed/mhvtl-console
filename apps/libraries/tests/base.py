"""
Base test class for all library tests
Handles common setup like user creation with custom User model
"""
from django.test import TestCase, Client
from django.contrib.auth import get_user_model

User = get_user_model()

class LibraryTestBase(TestCase):
    """Base class for all library tests with common setup"""
    
    @classmethod
    def setUpTestData(cls):
        """Set up data for the whole TestCase - runs once"""
        # Create a test user that all tests can use
        cls.test_user = User.objects.create_user(
            username='testuser',
            password='testpass123',
            email='test@test.com'
        )
        
        # Admin user for admin tests. The authentication app seeds a shared
        # 'admin' account in a migration, so adopt that row if it is there
        # rather than colliding with it on username.
        cls.admin_user, _ = User.objects.get_or_create(
            username='admin',
            defaults={'email': 'admin@test.com'},
        )
        cls.admin_user.is_staff = True
        cls.admin_user.is_superuser = True
        cls.admin_user.set_password('admin123')
        cls.admin_user.save()
    
    def setUp(self):
        """Set up for each test method"""
        self.client = Client()
        
    def login_as_user(self):
        """Helper method to login as regular user"""
        return self.client.login(username='testuser', password='testpass123')
    
    def login_as_admin(self):
        """Helper method to login as admin"""
        return self.client.login(username='admin', password='admin123')
    
    def assertRedirectsToLogin(self, response):
        """Helper to assert response redirects to login"""
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

