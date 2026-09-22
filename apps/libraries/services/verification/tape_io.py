"""Writing an archive to a tape device and reading it back.

Moved from backup_restore_test_service.py (the tar and mt parts of
backup_to_tape and restore_from_tape).

Three things about tape devices that the code has to get right, and which are
why this is its own module rather than two subprocess calls inside a workflow:

    /dev/stN rewinds on close and /dev/nstN does not. Writing through the
    rewinding node and then reading through it works by accident, because the
    rewind that happens on close is the one the read needed anyway. Writing two
    archives that way silently overwrites the first. The non-rewinding node is
    used where it exists, and the rewind is explicit.

    A rewind is not optional before reading. A tape left at end-of-data reads
    as an empty archive, and tar reports that as "This does not look like a tar
    archive" - which reads like corruption and is really just position.

    tar writing to a tape writes as root, so the restored files are root-owned
    and the checksum step cannot read them. Ownership is handed back after the
    extract.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import os
from pathlib import Path
from typing import Optional

from ..core import CommandResult, QUICK, shell
from ..operations import mt

logger = logging.getLogger(__name__)

#: tar to a tape is slow and the caller is a web request; ten minutes is long
#: enough for the sizes this harness generates and short enough that a wedged
#: drive does not hold a worker for ever.
ARCHIVE_TIMEOUT = 600


def writable_device(device_path: str) -> str:
    """The node to write through: the non-rewinding one where it exists.

    One rule, in operations/mt: /dev/st0 rewinds when it is closed, so a second
    archive written to it lands on top of the first. Kept as a name here because
    "writable device" is what this module means by it, and because the reason
    differs - for a write it is about overwriting, for a status read it is about
    not moving the tape while asking where it is.
    """
    return mt.non_rewinding(device_path)


def rewind(device_path: str) -> CommandResult:
    """Wind back to the start. A warning, not an error, if it fails.

    A drive with no tape loaded refuses, and so does one that is already there
    on some firmware; neither means the operation that follows cannot run.
    """
    result = shell.sudo(['mt', '-f', device_path, 'rewind'], timeout=60)
    if not result.ok:
        logger.warning('rewinding %s: %s', device_path,
                       result.output.strip()[:200])
    return result


def write_archive(device_path: str, source: Path) -> CommandResult:
    """tar the source directory onto the tape, from its parent.

    From the parent so the archive contains `source/...` rather than absolute
    paths: tar refuses to extract an absolute path without --absolute-names,
    and an archive that will not extract is not a verified backup.
    """
    source = Path(source)
    rewind(device_path)
    return shell.sudo(['tar', '-cvf', device_path, '-C', str(source.parent),
                       source.name], timeout=ARCHIVE_TIMEOUT)


def read_archive(device_path: str, target: Path) -> CommandResult:
    """Extract the tape's archive into target, then hand back ownership.

    The rewind matters: a tape sitting at end-of-data extracts nothing, and tar
    reports that as a malformed archive rather than an empty one.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)

    rewind(device_path)
    result = shell.sudo(['tar', '-xvf', device_path, '-C', str(target)],
                        timeout=ARCHIVE_TIMEOUT)
    if result.ok:
        _take_ownership(target)
    return result


def _take_ownership(target: Path) -> None:
    """tar wrote as root; the checksum step reads as the web user."""
    owner = f'{os.getuid()}:{os.getgid()}'
    result = shell.sudo(['chown', '-R', owner, str(target)], timeout=QUICK)
    if not result.ok:
        logger.warning('could not take ownership of %s: %s', target,
                       result.output.strip()[:200])


def extracted_root(target: Path) -> Path:
    """Where the files actually landed.

    write_archive stores the source directory by name, so an extract into
    `restore/` produces `restore/source/`. Returning the wrapper would make
    every file look missing.
    """
    target = Path(target)
    if not target.is_dir():
        # tar reported success and produced nothing, or the directory went away
        # between the extract and the check. Returning the path lets the
        # checksum step report every file missing, which is the truth.
        logger.warning('nothing was extracted into %s', target)
        return target

    entries = [entry for entry in target.iterdir() if entry.is_dir()]
    return entries[0] if len(entries) == 1 else target
