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

#: Which directory the RUNNING DAEMONS read, when that is not the one this
#: process is working in.
#:
#: Unset by default, and then the daemons are taken to read config_dir() - the
#: directory this process reads and writes. That is right for the web app,
#: whose setting IS the live directory, and for the test suite, whose setting is
#: a local copy that stands in for it.
#:
#: It is SET by the CLI whenever --config-dir points somewhere else
#: (mhvtl_cli/main.py), because then the two genuinely differ: the command works
#: on a copy while systemd keeps serving /etc/mhvtl. Without that,
#: daemons_are_ours() compared the copy against config_dir() - which
#: --config-dir had already moved to the copy - so it compared a directory with
#: itself and said yes. On 1 October 2026 that let add_ltfs_media_workflow
#: restart the live vtllibrary@60 from a scratch directory, which unloaded the
#: cartridge from drive 0 while an `ltfs` process still held the device.
#:
#: The same class of mistake cost three stopped units once before, which is why
#: the CLI defaults to /etc/mhvtl whatever the Django settings say.
DAEMON_CONFIG_DIR_SETTING = 'MHVTL_DAEMON_CONFIG_DIR'

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


def daemon_config_dir() -> Path:
    """The directory the running daemons read.

    config_dir() unless something has said otherwise, so the default is "the
    daemons read what we read" - true for the web app and for the tests. The
    CLI sets it when --config-dir makes the two differ. See
    DAEMON_CONFIG_DIR_SETTING for why that case needs saying out loud.
    """
    return Path(_setting(DAEMON_CONFIG_DIR_SETTING, str(config_dir())))


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
