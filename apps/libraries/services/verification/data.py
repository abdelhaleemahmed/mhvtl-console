"""Test data: generating it, checksumming it, and checking it came back.

Moved from backup_restore_test_service.py (generate_test_data, create_checksums,
verify_checksums).

The data is random bytes on purpose. A tape drive with compression enabled -
and MHVTL configures `Compression: factor 1 enabled 1` by default - will
compress a file of zeros to almost nothing, so a test written with zeros
verifies that compression works and says nothing about whether the tape holds
the data. Random bytes do not compress, so the number of bytes written is the
number of bytes generated.

SHA256 over the file contents, not size or mtime: tar preserves both of those
through a restore that has corrupted every byte in between.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

#: Read and write in 1MiB pieces: large enough that the syscall overhead does
#: not dominate, small enough that a 10GB test does not need 10GB of memory.
CHUNK_BYTES = 1024 * 1024

#: The checksum file travels with the data, so a restore can be verified
#: without the run that created it still being around.
MANIFEST_NAME = 'manifest.json'


def generate(target: Path, *, size_mb: int = 10, file_count: int = 5) -> List[Dict]:
    """Write `file_count` files of random data totalling roughly size_mb.

    Roughly, because the total is divided evenly and integer division loses the
    remainder - a 10MB test in 3 files writes 3 x 3.33MB. The exact size does
    not matter to the verification; what matters is that it is the same data
    coming back.
    """
    target.mkdir(parents=True, exist_ok=True)
    per_file = max((int(size_mb) * 1024 * 1024) // max(int(file_count), 1), 1)

    written = []
    for index in range(int(file_count)):
        name = f'test_file_{index + 1:03d}.dat'
        path = target / name
        with open(path, 'wb') as handle:
            remaining = per_file
            while remaining > 0:
                chunk = os.urandom(min(CHUNK_BYTES, remaining))
                handle.write(chunk)
                remaining -= len(chunk)
        written.append({'filename': name, 'size_bytes': per_file,
                        'path': str(path)})
    return written


def sha256_of(path: Path) -> str:
    """The file's SHA256, read in chunks so a large tape image fits in memory."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(8192), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checksums(directory: Path) -> Dict[str, Dict]:
    """Checksum every file in a directory, and write the manifest beside them.

    The manifest itself is excluded, for the obvious reason that its checksum
    cannot be inside it.
    """
    directory = Path(directory)
    found = {}
    for path in sorted(directory.glob('*')):
        if path.is_file() and path.name != MANIFEST_NAME:
            found[path.name] = {'sha256': sha256_of(path),
                                'size_bytes': path.stat().st_size}

    manifest = {'created_at': datetime.now().isoformat(), 'files': found,
                'total_files': len(found)}
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    return found


def verify(directory: Path, expected: Dict[str, Dict]) -> Tuple[List, List, List]:
    """Compare restored files against the checksums taken before the write.

    Returns (verified, corrupted, missing). Missing and corrupted are kept
    apart because they mean different things: a missing file is a tape that did
    not hold everything, a corrupted one is a tape that held it wrongly, and
    the second is the worse discovery.
    """
    directory = Path(directory)
    verified, corrupted, missing = [], [], []

    for name, original in expected.items():
        path = directory / name
        if not path.exists():
            missing.append(name)
            continue

        found = sha256_of(path)
        if found == original.get('sha256'):
            verified.append({'filename': name, 'sha256': found,
                             'size_bytes': path.stat().st_size})
        else:
            corrupted.append({'filename': name,
                              'expected_sha256': original.get('sha256'),
                              'found_sha256': found})

    return verified, corrupted, missing
