# mhvtl_system/settings/development.py
from .base import *
import os

DEBUG = True

# Override database for development with SQLite
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# The Django debug toolbar, for development only. It lived in base.py until
# now, which put it in INSTALLED_APPS and MIDDLEWARE for production and the
# test settings as well - along with a stray DEBUG = True.
#
# MHVTL_DEBUG_TOOLBAR=0 leaves it out: its panel covers the right quarter of
# the window, which matters when the browser test is recording the pages.
if os.environ.get('MHVTL_DEBUG_TOOLBAR', '1') != '0':
    INSTALLED_APPS += ['debug_toolbar']
    MIDDLEWARE += ['debug_toolbar.middleware.DebugToolbarMiddleware']
    INTERNAL_IPS = ['127.0.0.1']

# Development-specific settings
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0']
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# MHVTL Configuration Settings
#
# MHVTL_CONFIG_DIR is read from the environment (.env) so that the web UI and
# the CLI manage the same configuration. They did not until step 9: this file
# hardcoded ./generated_configs/, a copy taken at some point and drifting since,
# while the CLI and parts of the dashboard read /etc/mhvtl. A dashboard that
# reports its libraries from one directory and its tape counts from another is
# not obviously wrong on screen, which is why it went unnoticed.
#
# Point it back at a copy to develop against one - the services take a directory
# argument for exactly this - but do it deliberately:
#
#     MHVTL_CONFIG_DIR=./generated_configs/ python manage.py runserver
MHVTL_PRODUCTION_MODE = False
MHVTL_CONFIG_DIR = os.environ.get('MHVTL_CONFIG_DIR', '/etc/mhvtl')
MHVTL_ENABLE_SERVICE_CONTROL = False
MHVTL_HOME_DIR = os.environ.get('MHVTL_HOME_DIR', '/opt/mhvtl')

# Only create it when it is a relative development copy; /etc/mhvtl belongs to
# the package and a web process should not be making it.
if not os.path.isabs(MHVTL_CONFIG_DIR):
    os.makedirs(MHVTL_CONFIG_DIR, exist_ok=True)
# Cookies are scoped by host, not by port, so a dev server on 127.0.0.1:8099
# and an installed instance on 127.0.0.1:8080 would overwrite each other's
# session cookie: logging into one silently logs you out of the other, and
# visiting either bounces you to its login page. Give this instance its own
# cookie name so both can be used side by side.
SESSION_COOKIE_NAME = 'mhvtl_dev_sessionid'

# Where the console keeps the last reading of each drive, shared between
# processes. A development server writes it wherever it can.
MHVTL_GUI_STATE_DIR = os.environ.get('MHVTL_GUI_STATE_DIR', '/var/lib/mhvtl-gui')
