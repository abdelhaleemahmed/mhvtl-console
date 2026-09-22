# apps/libraries/apps.py
from django.apps import AppConfig


class LibrariesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.libraries'
    verbose_name = 'MHVTL Libraries'

    def ready(self):
        """Import signals when app is ready"""
        try:
            import apps.libraries.signals
        except ImportError:
            pass