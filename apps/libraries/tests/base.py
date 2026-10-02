"""Base test classes for the library tests.

Common setup - a user for the custom User model - and a temporary directory
that cleans itself up.

WHY tmpdir() EXISTS
-------------------
The suite used to call tempfile.mkdtemp directly, 89 times, and never
removed anything. Each run abandoned its directories; the ones where a test
had written a cartridge were 120 MB each, because a tape is a file. On this
host that reached 17.5 GB and 168,510 entries in /tmp - at which point no
glob worked any more (`rm -rf /tmp/tmp*` dies with "argument list too long")
and `du /tmp` had to walk the lot to tell you so.

So TestCase and SimpleTestCase here are Django's, plus tmpdir(). Importing
them from this module instead of django.test is what makes a test clean up
after itself, and test_tempdirs.py fails on any test module that calls
mkdtemp directly.
"""
import shutil
import tempfile
from pathlib import Path

from django.test import Client
from django.test import SimpleTestCase as DjangoSimpleTestCase
from django.test import TestCase as DjangoTestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class TempDirMixin:
    """A temporary directory that is removed when the test finishes."""

    def tmpdir(self, parent=None) -> Path:
        """A fresh empty directory, deleted after this test.

        ``parent`` nests it inside another directory - test_library_matrix
        makes one per library under a single base. The nested one is removed
        with its parent as well as on its own; both are registered, and a
        second removal of something already gone is not an error.

        ignore_errors because a test may legitimately have removed it
        already - test_lifecycle deletes the directory it was given - and a
        cleanup that raises would turn a passing test into an error.
        """
        path = Path(tempfile.mkdtemp(dir=parent))   # dir=None is the default
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


class TestCase(TempDirMixin, DjangoTestCase):
    """Django's TestCase, with tmpdir()."""


class SimpleTestCase(TempDirMixin, DjangoSimpleTestCase):
    """Django's SimpleTestCase, with tmpdir()."""


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

