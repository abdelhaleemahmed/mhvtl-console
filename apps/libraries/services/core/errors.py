"""Typed failures, so callers stop catching bare Exception.

New in the refactor. There are 103 bare `except Exception` blocks in the current
services and 124 in the views, which is how a missing method
(MHVTLLibraryService.add_drive) surfaced to the operator as "Error:" for months
instead of failing loudly.

Services catch these, turn them into a failure ServiceResult and log them; only
genuinely unexpected errors propagate.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""


class MhvtlError(Exception):
    """Base for everything this layer raises deliberately."""


class ConfigError(MhvtlError):
    """device.conf or library_contents is missing, unreadable or malformed."""


class DeviceNotFound(MhvtlError):
    """No SCSI device matches the address device.conf gives this library/drive.

    Raised rather than falling back to a positional guess: acting on the wrong
    library is worse than reporting that we cannot find the right one.
    """


class CommandFailed(MhvtlError):
    """An external command (mtx, vtlcmd, mktape, systemctl) returned non-zero."""

    def __init__(self, result):
        self.result = result
        super().__init__(f'{" ".join(result.argv)} failed: {result.output.strip()[:200]}')


class ValidationFailed(MhvtlError):
    """Caller-supplied data is not acceptable - a bad barcode, a taken slot."""
