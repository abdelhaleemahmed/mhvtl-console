# In mhvtl_system/settings.py

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party apps
    'rest_framework',

    # Local apps
    'apps.authentication',
    'apps.libraries',
]

# MHVTL Configuration Settings
MHVTL_PRODUCTION_MODE = False  # Set to True for production deployment
MHVTL_CONFIG_DIR = './generated_configs/' if not MHVTL_PRODUCTION_MODE else '/etc/mhvtl/'
MHVTL_ENABLE_SERVICE_CONTROL = MHVTL_PRODUCTION_MODE  # Only control services in production
MHVTL_HOME_DIR = '/opt/mhvtl'

# Create the generated_configs directory if in development mode
import os
if not MHVTL_PRODUCTION_MODE:
    os.makedirs(MHVTL_CONFIG_DIR, exist_ok=True)
