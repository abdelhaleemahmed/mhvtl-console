"""Shared plumbing: results, shell execution, locking, retries, paths, errors.

Every other package in services builds on this one, and this package depends on
nothing but the standard library (settings are read lazily, so it imports cleanly
without Django).

Modules:
    results.py    ServiceResult and ValidationResult - what every service returns
    shell.py      the only place a subprocess is started
    locking.py    FileLock and atomic_write_text for config writes
    paths.py      config and media directories, read from settings
    errors.py     typed failures
    retry.py      retrying transient SCSI busy states
"""
from .errors import (CommandFailed, ConfigError, DeviceNotFound, MhvtlError,
                     ValidationFailed)
from .locking import FileLock, LockTimeout, atomic_write_text
from .paths import (config_dir, device_conf_path, home_dir, library_contents_path,
                    lock_path, media_dir)
from .retry import is_transient, retry_on_busy
from .results import (ServiceResult, ValidationResult, failure_result,
                      success_result)
from .shell import NORMAL, QUICK, SLOW, CommandResult, run, sudo, sudo_cat, sudo_tee

__all__ = [
    'ServiceResult', 'ValidationResult',
    'success_result', 'failure_result',
    'run', 'sudo', 'sudo_cat', 'sudo_tee', 'CommandResult', 'QUICK', 'NORMAL', 'SLOW',
    'FileLock', 'LockTimeout', 'atomic_write_text',
    'config_dir', 'home_dir', 'device_conf_path', 'library_contents_path',
    'media_dir', 'lock_path',
    'retry_on_busy', 'is_transient',
    'MhvtlError', 'ConfigError', 'DeviceNotFound', 'CommandFailed', 'ValidationFailed',
]
