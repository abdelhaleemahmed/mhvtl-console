"""The last reading of each drive, shared by everything that asks.

MHVTL counts totals rather than a rate, so saying a drive is writing takes two
readings. Keeping the first one in a module dictionary looked enough and was
not: the console runs three gunicorn workers, each with its own memory, so the
first polls of a page landed on workers that had never seen the drive and
reported a drive that was plainly writing as merely holding a tape. The same
would go for `mhvtl status activity`, a separate process again.

One small file, written after each reading and read before the next, gives
every worker and every command the same previous reading.

Nothing here is a record of anything: it is a hint about the last few seconds,
thrown away when it gets old, and a missing or unreadable file only costs one
extra reading. That is why a failure to write is never raised - a page must
not break because a directory is read-only.

Rules for this layer:
    - returns plain values; a caller cannot tell a miss from a failure
    - never imports django.contrib.messages and never sees a request
"""
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = '/var/lib/mhvtl-gui'
FILE_NAME = 'drive-samples.json'

#: Readings older than this say nothing about now: a page polls every few
#: seconds, and a reading from two minutes ago would call a finished backup
#: "writing".
LIFE = 120.0

#: A reading is kept at least this long before a newer one replaces it.
#:
#: Without it, two people watching the same library interfere with each other:
#: the first request stores its reading, the second arrives a moment later,
#: finds a reading identical to its own and reports a drive that is plainly
#: writing as merely holding a tape. Both viewers then see the state flicker,
#: and how often depends on how many other people have the page open - which
#: is no way to decide what a drive is doing.
#:
#: Holding each reading for a few seconds means every request compares against
#: something old enough to have changed, however many requests there are.
MIN_AGE = 3.0

#: Two readings closer together than this say nothing about whether a drive is
#: moving: MHVTL updates its counters as buffers are flushed, so two reads a
#: fraction of a second apart can return the same number from a drive writing
#: at full speed. A caller that cannot tell keeps the last answer rather than
#: inventing a new one.
MIN_COMPARE = 1.0


def _setting(name: str, default: str) -> str:
    try:
        from django.conf import settings
        return getattr(settings, name, None) or default
    except Exception:            # noqa: BLE001 - no Django, or settings not ready
        return default


def path() -> Path:
    """The file the readings are kept in.

    MHVTL_GUI_STATE_DIR when it is set - the tests point it at a temporary
    directory - then /var/lib/mhvtl-gui, which the package creates, and
    finally the system temporary directory, so a development server run by a
    user who cannot write there still works.
    """
    folder = Path(_setting('MHVTL_GUI_STATE_DIR', DEFAULT_STATE_DIR))
    if not os.access(folder, os.W_OK):
        folder = Path(tempfile.gettempdir())
    return folder / FILE_NAME


def _read() -> Dict:
    try:
        return json.loads(path().read_text())
    except FileNotFoundError:
        return {}
    except Exception:            # noqa: BLE001 - a half-written or corrupt file
        logger.debug('drive samples could not be read', exc_info=True)
        return {}


def entry_for(key, now: float = None, life: float = LIFE) -> Optional[Dict]:
    """The stored entry for `key` - its reading, its age, and the state that
    was last decided from it - or None when there is nothing worth using."""
    stored = _read().get(str(key))
    if not stored:
        return None
    age = (now or time.time()) - stored.get('at', 0)
    if age > life:
        return None
    return {'reading': stored.get('reading'), 'age': age,
            'state': stored.get('state')}


def recent(key, now: float = None, life: float = LIFE) -> Optional[Dict]:
    """The last reading for `key`, if it is recent enough to compare with."""
    entry = entry_for(key, now=now, life=life)
    return entry['reading'] if entry else None


def remember(readings: Dict, now: float = None,
             min_age: float = MIN_AGE) -> None:
    """Store a reading per key, keeping the other keys that are still fresh.

    A key whose stored reading is younger than `min_age` is left alone: see
    MIN_AGE. Pass min_age=0 to store regardless.

    Written to a temporary file in the same directory and renamed, so a reader
    sees either the old file or the new one, never half of one.
    """
    when = now or time.time()
    kept = {key: entry for key, entry in _read().items()
            if when - entry.get('at', 0) <= LIFE}

    changed = False
    for key, reading in readings.items():
        name = str(key)
        existing = kept.get(name)
        state = reading.pop('state', None) if isinstance(reading, dict) else None

        if min_age and existing and when - existing.get('at', 0) < min_age:
            # Too young to replace: keep it as the thing the next request
            # compares against, but record what was decided this time.
            if state is not None and existing.get('state') != state:
                existing['state'] = state
                changed = True
            continue

        kept[name] = {'at': when, 'reading': reading, 'state': state}
        changed = True

    if not changed:
        return

    target = path()
    try:
        with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False,
                                         prefix=f'.{FILE_NAME}.') as handle:
            json.dump(kept, handle)
            temporary = Path(handle.name)
        _match_the_directory(temporary)
        temporary.replace(target)
    except Exception:            # noqa: BLE001 - read-only directory, full disk
        logger.debug('drive samples could not be written', exc_info=True)


def _match_the_directory(temporary: Path) -> None:
    """Give the file the directory's owner and group, and let that group write.

    The console runs as mhvtl-gui and `sudo mhvtl` runs as root, and they share
    this file. A temporary file is created 0600 and owned by whoever wrote it,
    so root writing first left a file the console could not read - and then
    neither could compare two readings.
    """
    try:
        owner = temporary.parent.stat()
        os.chmod(temporary, 0o664)
        if os.geteuid() == 0:
            os.chown(temporary, owner.st_uid, owner.st_gid)
    except Exception:            # noqa: BLE001 - not ours to give away
        logger.debug('drive samples could not be given the directory owner',
                     exc_info=True)


def forget() -> None:
    """Drop everything. For tests, and for a caller that wants a clean start."""
    try:
        path().unlink(missing_ok=True)
    except Exception:            # noqa: BLE001
        pass
