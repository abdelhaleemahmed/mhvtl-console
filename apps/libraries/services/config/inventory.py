"""Listing, reading and exporting the MHVTL configuration directory.

Moved out of views.py:1152-1305, where the directory listing, os.stat calls, the
filename allowlist and the ZIP builder all lived inline in three view methods -
and where the allowlist was written out three times and /etc/mhvtl hardcoded four
times, ignoring settings.MHVTL_CONFIG_DIR.

It also replaces the unreachable MHVTLConfigService.get_config_files_info() and
MHVTLFileDownloadService in apps/libraries/services.py, which the services/
package has shadowed since it was created: nothing could import them, and the
one command that tried (manage.py test_config_service) failed and returned
silently.

The allowlist is the security boundary: a name arrives from a URL, so anything
outside device.conf, mhvtl.conf and library_contents.N is refused, and the
resolved path is checked to be inside the config directory before it is read.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..core import config_dir as default_config_dir
from ..core import shell

logger = logging.getLogger(__name__)

#: Files this app will read or export. Everything else in the directory - backups,
#: editor leftovers, anything an operator dropped there - is ignored.
FIXED_FILES = ('device.conf', 'mhvtl.conf')
LIBRARY_CONTENTS_RE = re.compile(r'^library_contents\.\d+$')


def is_allowed(name: str) -> bool:
    """Is this a configuration file we are willing to touch?"""
    return name in FIXED_FILES or bool(LIBRARY_CONTENTS_RE.match(name))


@dataclass
class ConfigFile:
    """One file in the configuration directory."""
    name: str
    path: str
    size: int = 0
    modified: float = 0.0
    readable: bool = True
    kind: str = 'config'          # config | library_contents

    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'path': self.path,
            'size': self.size,
            'modified': self.modified,
            'readable': self.readable,
            'type': self.kind,
        }


def resolve(name: str, base: Path = None) -> Optional[Path]:
    """Turn a supplied file name into a path inside the config directory.

    Returns None for anything not on the allowlist, and for any name that
    resolves outside the directory - so '../../etc/shadow' is refused twice.
    """
    if not is_allowed(name):
        return None

    base = (base or default_config_dir()).resolve()
    candidate = (base / name).resolve()
    if candidate.parent != base:
        logger.warning('refusing path outside the config directory: %s', name)
        return None
    return candidate


def list_files(base: Path = None) -> List[ConfigFile]:
    """Every allowed file present, fixed ones first then library_contents."""
    base = base or default_config_dir()
    if not base.is_dir():
        return []

    found: List[ConfigFile] = []

    for name in FIXED_FILES:
        path = base / name
        if path.exists():
            found.append(_describe(path, 'config'))

    try:
        contents = sorted(name for name in os.listdir(base)
                          if LIBRARY_CONTENTS_RE.match(name))
    except OSError as exc:
        logger.warning('cannot list %s: %s', base, exc)
        contents = []

    for name in contents:
        found.append(_describe(base / name, 'library_contents'))

    return found


def _describe(path: Path, kind: str) -> ConfigFile:
    try:
        info = path.stat()
        return ConfigFile(name=path.name, path=str(path), size=info.st_size,
                          modified=info.st_mtime,
                          readable=os.access(path, os.R_OK), kind=kind)
    except OSError as exc:
        logger.warning('cannot stat %s: %s', path, exc)
        return ConfigFile(name=path.name, path=str(path), readable=False, kind=kind)


def read_file(name: str, base: Path = None) -> Optional[str]:
    """Read one allowed config file, using sudo only if a plain read fails.

    Returns None when the name is not allowed or the file does not exist, so a
    caller can tell "not permitted" from "empty".
    """
    path = resolve(name, base)
    if path is None or not path.exists():
        return None

    result = shell.sudo_cat(path)
    return result.stdout if result.ok else None


def export_zip(base: Path = None) -> bytes:
    """Every allowed config file, as a zip archive.

    Unreadable files are skipped rather than failing the whole export: an
    operator downloading a backup would rather have five of six files than an
    error page.
    """
    base = base or default_config_dir()
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for entry in list_files(base):
            text = read_file(entry.name, base)
            if text is None:
                logger.warning('skipping unreadable file in export: %s', entry.name)
                continue
            archive.writestr(entry.name, text)

    buffer.seek(0)
    return buffer.getvalue()
