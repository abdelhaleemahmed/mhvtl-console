#!/usr/bin/env python3
"""Django's command-line utility for administrative tasks."""
import os
import sys

if __name__ == '__main__':
    # `manage.py test` gets the testing settings, which point MHVTL_CONFIG_DIR
    # at a local copy. Development settings read the directory from the
    # environment and so, on this host, /etc/mhvtl - and a test suite that
    # creates and deletes libraries must never be pointed at the real one.
    # Before step 9 both were hardcoded to ./generated_configs/ and this did not
    # matter; now it does.
    default_settings = ('mhvtl_system.settings.testing' if 'test' in sys.argv[1:2]
                        else 'mhvtl_system.settings.development')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', default_settings)

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)
