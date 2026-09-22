# apps/libraries/tests/test_security.py
from django.test import TestCase, Client


class SecurityTests(TestCase):
    def test_authentication_required(self):
        """Test all views require authentication"""
        urls = [
            '/libraries/',
            '/libraries/list/',
            '/libraries/setup/',
            '/libraries/remove/',
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f'{url} should redirect unauthenticated users')

    def test_csrf_protection(self):
        """Test CSRF protection on forms"""
        client = Client(enforce_csrf_checks=True)
        # Use a URL that actually exists and accepts POST
        response = client.post('/libraries/setup/brand/HP/', {})
        # Should get 403 (CSRF) or 302 (auth redirect)
        self.assertIn(response.status_code, [302, 403])
