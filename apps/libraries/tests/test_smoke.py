"""
Smoke Test - Verify basic testing infrastructure works
Following Phase 1 of the Comprehensive Testing Plan
"""
from apps.libraries.tests.base import LibraryTestBase


class SmokeTest(LibraryTestBase):
    """Basic smoke tests to verify testing framework is operational"""

    def test_django_works(self):
        """Verify Django testing framework is operational"""
        self.assertEqual(1 + 1, 2)

    def test_can_import_views(self):
        """Verify we can import the views module"""
        try:
            from apps.libraries import views
            from apps.libraries import ajax_views
            self.assertTrue(True)
        except ImportError as e:
            self.fail(f"Cannot import views: {e}")

    def test_database_works(self):
        """Verify test database is working (SQLite)"""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.assertEqual(User.objects.count(), 2)
        self.assertTrue(User.objects.filter(username='testuser').exists())
        self.assertTrue(User.objects.filter(username='admin').exists())

    def test_client_works(self):
        """Verify test client can make requests"""
        response = self.client.get('/libraries/')
        self.assertIn(response.status_code, [302, 401])

    def test_authenticated_access(self):
        """Verify authentication works in tests"""
        login_success = self.login_as_user()
        self.assertTrue(login_success)

        response = self.client.get('/libraries/')
        # May return 200 or 302 depending on whether libraries exist
        self.assertIn(response.status_code, [200, 302])

    def test_admin_access(self):
        """Verify admin authentication works"""
        login_success = self.login_as_admin()
        self.assertTrue(login_success)

        response = self.client.get('/libraries/')
        self.assertIn(response.status_code, [200, 302])
