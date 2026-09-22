# mhvtl_system/settings/testing.py
from .base import *
import os

DEBUG = False

# Use in-memory SQLite for fast tests
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Faster password hashing for tests
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# MHVTL — disable production mode and service control during tests
#
# MHVTL_CONFIG_DIR deliberately does NOT read the environment, unlike
# development.py. The suite creates and deletes libraries, and a test run
# pointed at /etc/mhvtl would do it to the real one. Most tests pass an
# explicit scratch directory; this is the backstop for any that forget.
# manage.py selects these settings for `test`.
#
# It is a fresh copy of the test fixtures, not ./generated_configs/. That was
# a directory the development server happens to create, so the suite passed
# on a machine where somebody had run one and failed on a clean checkout -
# two tests read a device.conf that was not there. A backstop that exists
# only on some machines is not a backstop.
import shutil as _shutil
import tempfile as _tempfile
from pathlib import Path as _Path

MHVTL_PRODUCTION_MODE = False

_FIXTURES = _Path(__file__).resolve().parents[2] / 'apps' / 'libraries' / 'tests' / 'fixtures'
MHVTL_CONFIG_DIR = _tempfile.mkdtemp(prefix='mhvtl-test-config-')
for _name in ('device.conf', 'library_contents.10', 'library_contents.20',
              'library_contents.30'):
    if (_FIXTURES / _name).is_file():
        _shutil.copy(_FIXTURES / _name, MHVTL_CONFIG_DIR)
MHVTL_ENABLE_SERVICE_CONTROL = False
MHVTL_HOME_DIR = '/opt/mhvtl'
# iscsi/bindings.py rebinds live iSCSI backstores after a daemon restart. A test
# that mocked the restart but not the rebind once changed library 10's real
# export, so it is off for the whole suite; its own tests turn it on around
# mocked commands.
MHVTL_ISCSI_REBIND = False


# The drive readings the console shares between its workers: a temporary
# directory, never /var/lib/mhvtl-gui, which belongs to the installed console.
import tempfile as _tempfile
MHVTL_GUI_STATE_DIR = _tempfile.mkdtemp(prefix='mhvtl-test-state-')

# Whitenoise needs STATIC_ROOT to exist; use a temp path for tests
import tempfile
STATIC_ROOT = os.path.join(tempfile.gettempdir(), 'mhvtl_test_static')
os.makedirs(STATIC_ROOT, exist_ok=True)

# Disable logging noise during tests
LOGGING = {}
