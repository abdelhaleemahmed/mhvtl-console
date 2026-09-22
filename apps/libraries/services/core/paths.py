"""Where MHVTL keeps things, read from settings instead of hardcoded.

Moved from tape_operations_service.py:381-387, which was added while fixing the
move-tape page: the service hardcoded config_dir='/etc/mhvtl' and ignored
settings, so any instance that could not read that directory failed every device
lookup with "Could not find device for library N". The same literals appear four
times in views.py and three times in ajax_views.py.

Settings are read lazily, inside the functions, so a CLI can import the service
layer and override paths without Django being configured first.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from pathlib import Path

DEFAULT_CONFIG_DIR = '/etc/mhvtl'
DEFAULT_HOME_DIR = '/opt/mhvtl'

#: Name of the lock guarding every configuration write. One lock for the whole
#: directory: a library change touches device.conf and library_contents together.
LOCK_NAME = '.mhvtl-config.lock'


def _setting(name: str, default: str) -> str:
    """Read a Django setting if Django is configured, else fall back."""
    try:
        from django.conf import settings
        return getattr(settings, name, None) or default
    except Exception:            # noqa: BLE001 - no Django, or settings not ready
        return default


def config_dir() -> Path:
    """Where device.conf and library_contents.N live."""
    return Path(_setting('MHVTL_CONFIG_DIR', DEFAULT_CONFIG_DIR))


def home_dir() -> Path:
    """Where the per-tape media directories live."""
    return Path(_setting('MHVTL_HOME_DIR', DEFAULT_HOME_DIR))


def _as_dir(base, default) -> Path:
    """Accept a str, a Path or None. Callers - especially the CLI - pass all three."""
    return Path(base) if base else default()


def device_conf_path(base=None) -> Path:
    return _as_dir(base, config_dir) / 'device.conf'


def library_contents_path(library_id: int, base=None) -> Path:
    return _as_dir(base, config_dir) / f'library_contents.{library_id}'


def media_dir(barcode: str, base=None) -> Path:
    """The directory holding one tape's data files."""
    return _as_dir(base, home_dir) / barcode


def lock_path(base=None) -> Path:
    return _as_dir(base, config_dir) / LOCK_NAME
