# mhvtl_system/settings/production.py
from .base import *
import os

DEBUG = False

# Load environment variables
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost'])

# Security settings for production.
#
# HTTPS is the default here: the installer puts nginx in front with a
# certificate, and the three settings below only make sense together. They
# used to default to False, which left `manage.py check --deploy` warning
# about all three on every install.
#
# HTTPS=0 in the environment turns the set off in one move, for a host that
# really is served over plain HTTP - the cookies would otherwise never be
# sent and nobody could log in. Each setting can still be overridden on its
# own for an unusual arrangement.
HTTPS = env.bool('HTTPS', default=True)

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = env.bool('SECURE_SSL_REDIRECT', default=HTTPS)
SECURE_HSTS_SECONDS = env.int('SECURE_HSTS_SECONDS', default=31536000 if HTTPS else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS
SECURE_HSTS_PRELOAD = HTTPS
X_FRAME_OPTIONS = 'DENY'
CSRF_COOKIE_SECURE = env.bool('CSRF_COOKIE_SECURE', default=HTTPS)
SESSION_COOKIE_SECURE = env.bool('SESSION_COOKIE_SECURE', default=HTTPS)

# nginx terminates the TLS and proxies to gunicorn over plain HTTP; without
# this Django sees http:// and SECURE_SSL_REDIRECT loops for ever.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# The address the browser is sent to, so a POST from https:// is accepted.
# Django compares the whole origin, port included, and nginx serves 8443 -
# an origin without the port matches nothing and every POST is rejected.
HTTPS_PORT = env.int('HTTPS_PORT', default=443)
_ORIGIN_PORT = '' if HTTPS_PORT == 443 else f':{HTTPS_PORT}'
CSRF_TRUSTED_ORIGINS = env.list(
    'CSRF_TRUSTED_ORIGINS',
    default=[f'https://{host}{_ORIGIN_PORT}'
             for host in ALLOWED_HOSTS if host not in ('*',)])

# Database configuration - SQLite for simplicity as requested
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            'timeout': 30,
        },
    }
}

# Static files configuration
STATIC_ROOT = env('STATIC_ROOT', default='/var/www/mhvtl/static/')
MEDIA_ROOT = env('MEDIA_ROOT', default='/var/www/mhvtl/media/')

# Email configuration
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = env('EMAIL_HOST', default='localhost')
EMAIL_PORT = env.int('EMAIL_PORT', default=587)
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=True)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')

# Cache configuration
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': '/var/tmp/django_cache',
    }
}

# Session configuration
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'

# Logging configuration
LOG_DIR = env('LOG_DIR', default='/var/log/mhvtl/')
os.makedirs(LOG_DIR, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'django.log'),
            'maxBytes': 10485760,  # 10MB
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'django_errors.log'),
            'maxBytes': 10485760,  # 10MB
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'mhvtl_file': {
            'level': 'DEBUG',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'mhvtl_operations.log'),
            'maxBytes': 10485760,  # 10MB
            'backupCount': 10,
            'formatter': 'verbose',
        },
        # NEW: Script service logging
        'mhvtl_script_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'mhvtl_scripts.log'),
            'maxBytes': 10485760,  # 10MB
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
        'django.request': {
            'handlers': ['error_file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'apps': {
            'handlers': ['file', 'console'],
            'level': 'DEBUG',
            'propagate': True,
        },
        'mhvtl_operations': {
            'handlers': ['mhvtl_file', 'console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        # NEW: Script service logger
        'apps.libraries.services.mhvtl_script_service': {
            'handlers': ['mhvtl_script_file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
    },
}

# =============================================================================
# MHVTL 1.7.1 PRODUCTION CONFIGURATION
# =============================================================================

# Enable production mode - This will write to real MHVTL directories
MHVTL_PRODUCTION_MODE = env.bool('MHVTL_PRODUCTION_MODE', default=True)

# MHVTL system directories (confirmed from your package structure)
MHVTL_CONFIG_DIR = env('MHVTL_CONFIG_DIR', default='/etc/mhvtl/')
MHVTL_HOME_DIR = env('MHVTL_HOME_DIR', default='/opt/mhvtl/')
MHVTL_MEDIA_DIR = env('MHVTL_MEDIA_DIR', default='/opt/mhvtl/')  # Media is directly in /opt/mhvtl/
MHVTL_BACKUP_DIR = env('MHVTL_BACKUP_DIR', default='/var/backups/mhvtl/')

# Where the console keeps the little it has to remember between requests - the
# last reading of each drive, so three gunicorn workers and the CLI share one
# answer about what a drive is doing. Nothing in here is worth keeping.
MHVTL_GUI_STATE_DIR = env('MHVTL_GUI_STATE_DIR', default='/var/lib/mhvtl-gui')

# Service control - Enable real MHVTL service management
MHVTL_ENABLE_SERVICE_CONTROL = env.bool('MHVTL_ENABLE_SERVICE_CONTROL', default=True)

# Command execution - Enable real system commands
MHVTL_ENABLE_COMMAND_EXECUTION = env.bool('MHVTL_ENABLE_COMMAND_EXECUTION', default=True)

# =============================================================================
# NEW: MHVTL SCRIPT SERVICE CONFIGURATION
# =============================================================================

# Script execution settings
MHVTL_SCRIPTS_PATH = env('MHVTL_SCRIPTS_PATH', default='/usr/bin')
MHVTL_SCRIPT_TIMEOUT = env.int('MHVTL_SCRIPT_TIMEOUT', default=30)
MHVTL_SUDO_REQUIRED = env.bool('MHVTL_SUDO_REQUIRED', default=True)
MHVTL_REQUIRED_VERSION = env('MHVTL_REQUIRED_VERSION', default='1.7.0')
MHVTL_HEALTH_CHECK_INTERVAL = env.int('MHVTL_HEALTH_CHECK_INTERVAL', default=300)

# Script service integration settings
MHVTL_SCRIPT_SERVICE = {
    'timeout': MHVTL_SCRIPT_TIMEOUT,
    'sudo_required': MHVTL_SUDO_REQUIRED,
    'config_dir': MHVTL_CONFIG_DIR,
    'data_dir': MHVTL_HOME_DIR,
    'scripts_path': MHVTL_SCRIPTS_PATH,
    'required_scripts': [
        'generate_device_conf',
        'vtlcmd',
        'make_vtl_media',
        'update_device.conf',
        'vtllibrary',
        'vtltape',
    ],
}

# MHVTL binary paths (confirmed from your package structure)
# UPDATED: Extended with script service requirements
MHVTL_BINARIES = {
    'vtlcmd': env('MHVTL_VTLCMD_PATH', default='/usr/bin/vtlcmd'),
    'mtx': env('MHVTL_MTX_PATH', default='/usr/bin/mtx'),  # May need separate install
    'edit_tape': env('MHVTL_EDIT_TAPE_PATH', default='/usr/bin/edit_tape'),
    'make_vtl_media': env('MHVTL_MAKE_VTL_MEDIA_PATH', default='/usr/bin/make_vtl_media'),
    'dump_tape': env('MHVTL_DUMP_TAPE_PATH', default='/usr/bin/dump_tape'),
    'generate_device_conf': env('MHVTL_GENERATE_DEVICE_CONF_PATH', default='/usr/bin/generate_device_conf'),
    'generate_library_contents': env('MHVTL_GENERATE_LIBRARY_CONTENTS_PATH', default='/usr/bin/generate_library_contents'),
    'vtllibrary': env('MHVTL_VTLLIBRARY_PATH', default='/usr/bin/vtllibrary'),
    'vtltape': env('MHVTL_VTLTAPE_PATH', default='/usr/bin/vtltape'),
    'mktape': env('MHVTL_MKTAPE_PATH', default='/usr/bin/mktape'),
    'preload_tape': env('MHVTL_PRELOAD_TAPE_PATH', default='/usr/bin/preload_tape'),
    'update_device_conf': env('MHVTL_UPDATE_DEVICE_CONF_PATH', default='/usr/bin/update_device.conf'),
}

# Systemd service management (MHVTL 1.7.1 uses systemd)
MHVTL_SYSTEMD_SERVICES = {
    'main_target': 'mhvtl.target',
    'library_service': 'vtllibrary@{}.service',  # Template service
    'tape_service': 'vtltape@{}.service',        # Template service
    'load_modules': 'mhvtl-load-modules.service',
}

# Sudo configuration for MHVTL operations
MHVTL_SUDO_USER = env('MHVTL_SUDO_USER', default='root')  # MHVTL typically runs as root
MHVTL_SUDO_ENABLED = env.bool('MHVTL_SUDO_ENABLED', default=True)

# MHVTL configuration file templates
MHVTL_CONFIG_TEMPLATES = {
    'device_conf': os.path.join(MHVTL_CONFIG_DIR, 'device.conf'),
    'mhvtl_conf': os.path.join(MHVTL_CONFIG_DIR, 'mhvtl.conf'),
    'library_contents': os.path.join(MHVTL_CONFIG_DIR, 'library_contents.{}'),
}

# Safety settings
MHVTL_REQUIRE_CONFIRMATION = env.bool('MHVTL_REQUIRE_CONFIRMATION', default=True)
MHVTL_BACKUP_CONFIGS = env.bool('MHVTL_BACKUP_CONFIGS', default=True)

# Create required directories
for directory in [MHVTL_BACKUP_DIR, LOG_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
    except PermissionError:
        # Log the error but don't fail startup
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Cannot create directory {directory} - may need manual creation with proper permissions")

# MHVTL system status monitoring
MHVTL_MONITORING = {
    'CHECK_SERVICES': env.bool('MHVTL_CHECK_SERVICES', default=True),
    'CHECK_DEVICES': env.bool('MHVTL_CHECK_DEVICES', default=True),
    'CHECK_MEDIA': env.bool('MHVTL_CHECK_MEDIA', default=True),
    'STATUS_CACHE_TIMEOUT': env.int('MHVTL_STATUS_CACHE_TIMEOUT', default=30),
    'USE_SYSTEMD': True,  # MHVTL 1.7.1 uses systemd
}

# Performance settings
# UPDATED: Enhanced with script service performance settings
MHVTL_PERFORMANCE = {
    'CONCURRENT_OPERATIONS': env.int('MHVTL_CONCURRENT_OPERATIONS', default=3),
    'OPERATION_TIMEOUT': env.int('MHVTL_OPERATION_TIMEOUT', default=300),
    'COMMAND_TIMEOUT': env.int('MHVTL_COMMAND_TIMEOUT', default=60),
    'SCRIPT_RETRY_COUNT': env.int('MHVTL_SCRIPT_RETRY_COUNT', default=3),
    'SCRIPT_RETRY_DELAY': env.int('MHVTL_SCRIPT_RETRY_DELAY', default=5),
}

# Media type definitions based on your existing media
MHVTL_MEDIA_TYPES = {
    'LTO8': {
        'identifier': 'L8',
        'capacity_mb': 12000000,  # 12TB native
        'block_size': 524288,
        'description': 'LTO Ultrium 8',
    },
    'LTO6': {
        'identifier': 'L6', 
        'capacity_mb': 2500000,  # 2.5TB native
        'block_size': 524288,
        'description': 'LTO Ultrium 6',
    },
    'LTO5': {
        'identifier': 'L5',
        'capacity_mb': 1500000,  # 1.5TB native  
        'block_size': 524288,
        'description': 'LTO Ultrium 5',
    },
    'T10KTA': {
        'identifier': 'TA',
        'capacity_mb': 500000,  # 500GB native
        'block_size': 262144,
        'description': 'T10000 VolSafe',
    },
}

# Existing media detection - scan /opt/mhvtl/ for existing tapes
MHVTL_SCAN_EXISTING_MEDIA = env.bool('MHVTL_SCAN_EXISTING_MEDIA', default=True)

# =============================================================================
# SCRIPT SERVICE INTEGRATION SETTINGS
# =============================================================================

# Discovery service integration with script operations
MHVTL_DISCOVERY_INTEGRATION = {
    'AUTO_SYNC_AFTER_SCRIPT': env.bool('MHVTL_AUTO_SYNC_AFTER_SCRIPT', default=True),
    'SYNC_TIMEOUT': env.int('MHVTL_SYNC_TIMEOUT', default=60),
    'VERIFY_AFTER_CREATION': env.bool('MHVTL_VERIFY_AFTER_CREATION', default=True),
    'ROLLBACK_ON_FAILURE': env.bool('MHVTL_ROLLBACK_ON_FAILURE', default=True),
}

# Error handling and validation
MHVTL_ERROR_HANDLING = {
    'STRICT_VALIDATION': env.bool('MHVTL_STRICT_VALIDATION', default=True),
    'LOG_ALL_OPERATIONS': env.bool('MHVTL_LOG_ALL_OPERATIONS', default=True),
    'NOTIFY_FAILURES': env.bool('MHVTL_NOTIFY_FAILURES', default=True),
    'PRESERVE_FAILED_CONFIGS': env.bool('MHVTL_PRESERVE_FAILED_CONFIGS', default=True),
}

# Development vs Production behavior for script service
if MHVTL_PRODUCTION_MODE:
    # Production: Use real MHVTL directories and commands
    MHVTL_SCRIPT_CONFIG_DIR = MHVTL_CONFIG_DIR
    MHVTL_SCRIPT_DATA_DIR = MHVTL_HOME_DIR
else:
    # Development: Use safe local directories (if running in development mode)
    MHVTL_SCRIPT_CONFIG_DIR = os.path.join(BASE_DIR, 'generated_configs')
    MHVTL_SCRIPT_DATA_DIR = os.path.join(BASE_DIR, 'generated_data')
    
    # Ensure development directories exist
    os.makedirs(MHVTL_SCRIPT_CONFIG_DIR, exist_ok=True)
    os.makedirs(MHVTL_SCRIPT_DATA_DIR, exist_ok=True)
