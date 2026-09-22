"""Drive CRUD - the role that had no module before the refactor.

The logic here is moved, not rewritten, from
backup_mhvtl_library_service.py:1451-1881 (list_drives, get_drive, add_drive,
remove_drive, update_drive and their helpers). That file has zero importers, yet
it was the only place these existed: tape_operations_views.py:1138, :1205, :1410
and :1444 call add_drive/remove_drive on MHVTLLibraryService, which defines
neither, so every one raised AttributeError and the operator saw "Error:".
Adding or removing a drive in the web UI has never worked.

What changed on the way across, and why:

    - results come from core.results, so a caller cannot tell which service it
      called from the shape of the answer;
    - writes go through core.locking: read, modify, write a temp file, fsync,
      rename, all under one lock. The original appended to device.conf in place,
      which interleaves badly with a concurrent library change;
    - reads and sudo go through core.shell, so there is one timeout policy.

device.conf parsing and writing go through services/config, which owns the
file format; this module only decides what a drive record should contain.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..config import device_conf as device_conf_format
from ..config import ids
from ..config import library_contents as library_contents_format
from ..core import (FileLock, ServiceResult, atomic_write_text, config_dir,
                    device_conf_path, failure_result, library_contents_path,
                    lock_path, shell, success_result)
from ..console import units
from ..iscsi import bindings as iscsi_bindings
from ..operations import vtlcmd
from ..profiles import data as profiles_data
from ..core import samples
from ..core.units import fullness as fullness_of
from ..core.units import human_size
from ..profiles import personalities
from .models import DriveInfo

logger = logging.getLogger(__name__)

#: How long a reading says anything about now, and where readings are kept.
#: core.samples holds them in a file rather than in this process: the console
#: runs three gunicorn workers and the CLI is a fourth process, and a reading
#: one of them took is exactly what the next one needs.
_SAMPLE_LIFE = samples.LIFE


def _reading(entry):
    """The TapeStats in a stored entry, or None when there is not one."""
    stored = (entry or {}).get('reading')
    if not stored:
        return None
    return vtlcmd.TapeStats(
        barcode=stored.get('barcode'), loaded=bool(stored.get('loaded')),
        written=stored.get('written', 0), read=stored.get('read', 0),
        written_media=stored.get('written_media', 0),
        read_media=stored.get('read_media', 0),
        capacity=stored.get('capacity', 0))


def _describe(current, before, *, last_state: str = None,
              comparable: bool = True) -> Dict:
    """What a drive is doing, in the words the page and the CLI both print.

    `current` and `before` are TapeStats (or None). MHVTL counts totals, so a
    drive is writing when the second reading is larger than the first; with
    only one reading there is nothing to compare and the honest answer is that
    it holds a tape.

    `comparable` is False when the two readings are too close together in time
    to mean anything - MHVTL updates its counters as buffers are flushed, so
    two reads a fraction of a second apart can return the same number from a
    drive writing at full speed. Then `last_state`, the answer worked out when
    there was something to compare, is kept: a drive does not stop and start
    within a second, and saying it did because two viewers happened to ask at
    once is worse than saying nothing new.
    """
    if current is None:
        return {'state': 'silent', 'label': 'the daemon did not answer',
                'detail': '', 'barcode': None, 'percent': None,
                'fullness': 'unknown'}
    if not current.loaded:
        return {'state': 'empty', 'label': 'empty', 'detail': '',
                'barcode': None, 'percent': None, 'fullness': 'unknown'}

    state = 'holding'
    if before is not None and current.written > before.written:
        state = 'writing'
    elif before is not None and current.read > before.read:
        state = 'reading'
    elif not comparable and last_state in ('writing', 'reading'):
        state = last_state

    detail = f'{human_size(current.written)} written'
    if current.read:
        detail += f', {human_size(current.read)} read'
    if current.used_percent is not None:
        detail += f' - {current.used_percent}% of the tape'
    # MHVTL reports 1.0 for data that did not compress, which is most of a
    # test run and not worth a clause.
    ratio = current.compression_ratio
    if ratio and abs(ratio - 1) >= 0.05:
        detail += f', {ratio}x compression'

    # How full the tape is, and how alarming that should look. The same two
    # values the tape tiles use, from the same rule in core.units, so a tape
    # is not described one way in a drive and another in its slot.
    percent = current.used_percent
    return {'state': state, 'barcode': current.barcode,
            'label': f'{state} {current.barcode}', 'detail': detail,
            'percent': percent, 'fullness': fullness_of(percent)}


#: Defaults for a new drive, matching what MHVTL's own generator writes.
DEFAULT_VENDOR = 'IBM'
DEFAULT_PRODUCT = 'ULT3580-TD8'


class DriveService:
    """Drives, as described by device.conf.

        DriveService().list(library_id=10)
        DriveService().add(10, {'vendor': 'IBM', 'product': 'ULT3580-TD8'})
    """

    def __init__(self, config_directory=None):
        self.config_dir = Path(config_directory) if config_directory else config_dir()
        self.device_conf = device_conf_path(self.config_dir)

    # -- reading ----------------------------------------------------------

    def _parse_config(self) -> Dict:
        """Read and parse device.conf.

        Reading lives here rather than in the parser so that the parser stays a
        pure text-to-data function, and so a root-only device.conf still works.
        """
        result = shell.sudo_cat(self.device_conf)
        if not result.ok:
            raise FileNotFoundError(
                f'cannot read {self.device_conf}: {result.stderr.strip()}')
        return device_conf_format.parse(result.stdout).to_dict()

    def list(self, library_id: int = None) -> ServiceResult:
        """Every drive, or just one library's drives."""
        operation_id = str(uuid.uuid4())[:8]
        try:
            config = self._parse_config()
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.error('listing drives: %s', exc)
            return failure_result(f'Could not read the drive configuration: {exc}',
                                  [str(exc)], operation_id)

        drives = [DriveInfo.from_config(drive_id, data)
                  for drive_id, data in config.get('drives', {}).items()
                  if library_id is None or data.get('library_id') == library_id]
        drives.sort(key=lambda drive: drive.drive_id)

        scope = f' in library {library_id}' if library_id is not None else ''
        return success_result(
            f'Found {len(drives)} drive{"s" if len(drives) != 1 else ""}{scope}',
            {'drives': [drive.to_dict() for drive in drives], 'count': len(drives)},
            operation_id)

    def get(self, drive_id: int) -> ServiceResult:
        """One drive by its queue id."""
        operation_id = str(uuid.uuid4())[:8]
        try:
            config = self._parse_config()
        except Exception as exc:                       # noqa: BLE001 - reported
            return failure_result(f'Could not read the drive configuration: {exc}',
                                  [str(exc)], operation_id)

        data = config.get('drives', {}).get(drive_id)
        if data is None:
            return failure_result(f'Drive {drive_id} not found',
                                  [f'No drive with id {drive_id} in {self.device_conf}'],
                                  operation_id)

        return success_result(f'Drive {drive_id}',
                              {'drive': DriveInfo.from_config(drive_id, data).to_dict()},
                              operation_id)

    # -- writing ----------------------------------------------------------

    def _apply(self, library_id: int, *, start: int = None, stop: int = None):
        """Make the running daemons match device.conf, and say what happened.

        vtllibrary reads its drive count and element layout once, at start, so
        a drive added or removed is invisible until it restarts; the drive's
        own vtltape unit has to be started or stopped as well. add() and
        remove() used to do neither and tell the operator to restart MHVTL by
        hand, so a drive added from the page did not appear at all.

        Returns (note, done): the note goes on the end of the message, and done
        is False when the daemons were not made to match, so the caller can
        still say a restart is needed.
        """
        if Path(self.config_dir).resolve() != Path(config_dir()).resolve():
            # The daemons run from the live configuration; this instance is
            # writing somewhere else (a test, a scratch copy), and restarting
            # them would apply a file they do not read.
            return (f'; the daemons were left alone: {self.config_dir} is not '
                    f'the live configuration directory', False)

        notes = []
        try:
            if stop is not None:
                unit = f'vtltape@{int(stop)}.service'
                units.stop(unit)
                units.disable(unit)
                # Otherwise systemd keeps the instance in "failed" - the drive
                # is gone, and the console reports a unit that will not start.
                units.reset_failed(unit)
                notes.append(f'{unit} stopped')
            if start is not None:
                started = units.start_library(library_id, [int(start)])
                failed = [unit for unit, ok in started.items() if not ok]
                if failed:
                    return f'; {", ".join(failed)} did not start, start them by hand', False
                notes.append(f'vtltape@{int(start)}.service started')
            outcome = units.restart_library(int(library_id))
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.warning('restarting library %s: %s', library_id, exc)
            return f'; the daemons did not restart ({exc}), restart them by hand', False

        if not outcome['ok']:
            return (f'; the daemons did not restart '
                    f'({outcome.get("error") or "systemctl reported a failure"}), '
                    f'restart them by hand', False)
        notes.append(f'{outcome["restarted"]} restarted')
        # The restart re-created the changer, and a stopped drive took its
        # device with it: rebind whatever this library exports over iSCSI.
        exports = iscsi_bindings.after_restart(int(library_id),
                                               config_directory=self.config_dir)
        if exports:
            notes.append(exports)
        return '; ' + ', '.join(notes), True

    def activity(self, library_id: int, *, settle: float = 1.0) -> ServiceResult:
        """What each of a library's drives is doing, live and already worded.

        The counters come from `vtlcmd <drive> stats`, which asks the daemon
        over its message queue and so answers while a backup is running - mt
        and mtx cannot, because the kernel holds the SCSI reservation for the
        initiator. That command is our own addition to MHVTL (patches/0004).

        MHVTL reports totals, not a rate, so "writing" is two readings with a
        larger number in the second. Which is why the deciding happens here
        rather than in the page: `mhvtl status activity` has to reach the same
        answer, and a browser is not the only thing that asks.

        The last reading of each drive is kept, so a caller that asks again
        within _SAMPLE_LIFE seconds gets its comparison for free. A caller
        with nothing to compare against - a command run once - waits `settle`
        seconds and reads a second time. Pass settle=0 to refuse that wait and
        get "holding" instead of "writing".

        Every drive gets a row, tape or no tape.
        """
        operation_id = str(uuid.uuid4())[:8]
        listed = self.list(library_id=int(library_id))
        if not listed.success:
            return listed

        ids = [drive['drive_id'] for drive in listed.data['drives']]
        now = time.time()
        first = {drive_id: vtlcmd.stats(drive_id) for drive_id in ids}

        # Whatever we have no fresh previous reading for is read again, once,
        # after a single wait - not one wait per drive.
        unknown = [drive_id for drive_id in ids
                   if samples.recent(drive_id, now=now, life=_SAMPLE_LIFE) is None
                   and first[drive_id]]
        second = {}
        if unknown and settle:
            time.sleep(settle)
            second = {drive_id: vtlcmd.stats(drive_id) for drive_id in unknown}

        drives = []
        latest = {}
        for drive in listed.data['drives']:
            drive_id = drive['drive_id']
            stored = samples.entry_for(drive_id, now=now, life=_SAMPLE_LIFE)
            if drive_id in second:
                before = first[drive_id]
                current = second[drive_id]
                comparable = bool(settle and settle >= samples.MIN_COMPARE)
                last_state = None
            else:
                before = _reading(stored)
                current = first[drive_id]
                comparable = bool(stored and stored['age'] >= samples.MIN_COMPARE)
                last_state = (stored or {}).get('state')

            described = _describe(current, before, last_state=last_state,
                                  comparable=comparable)
            if current:
                latest[drive_id] = {**current.to_dict(),
                                    'state': described['state']}
            drives.append({
                'drive_id': drive_id, 'slot': drive.get('slot'),
                'vendor': drive.get('vendor'), 'product': drive.get('product'),
                'serial': drive.get('serial'),
                'stats': current.to_dict() if current else None,
                **described,
            })

        if latest:
            samples.remember(latest)

        loaded = [d for d in drives if (d['stats'] or {}).get('loaded')]
        busy = [d for d in drives if d['state'] in ('writing', 'reading')]
        message = f'{len(loaded)} of {len(drives)} drive(s) hold a tape'
        if busy:
            message += f', {len(busy)} working'
        return success_result(
            message,
            {'library_id': int(library_id), 'drives': drives,
             'loaded': len(loaded), 'busy': len(busy), 'total': len(drives)},
            operation_id)

    def placement(self, library_id: int, product: str = None) -> ServiceResult:
        """Where the next drive would land, and which models this library takes.

        What add() would do, without doing it: the slot, the drive id, the SCSI
        target and the serial it would be given, the drives the library's
        profile allows, and whether there is room for another at all. The page
        shows this before the operator commits, and add() computes it the same
        way, so the two cannot disagree.
        """
        operation_id = str(uuid.uuid4())[:8]
        config = self._parse_config()
        library = config.get('libraries', {}).get(int(library_id))
        if library is None:
            return failure_result(f'Library {library_id} not found',
                                  ['it is not declared in device.conf'], operation_id)

        current = shell.sudo_cat(self.device_conf)
        if not current.ok:
            return failure_result(f'Could not read {self.device_conf}',
                                  [current.stderr.strip()], operation_id)
        conf = device_conf_format.parse(current.stdout)

        vendor, model = library.get('vendor', ''), library.get('product', '')
        existing = {drive_id: data
                    for drive_id, data in config.get('drives', {}).items()
                    if data.get('library_id') == int(library_id)}
        layout = personalities.library_layout(vendor, model)
        slot = self._next_free_slot(existing)

        sibling = next(iter(sorted(existing.items())), (None, {}))[1]
        supported = self._allowed_drives(vendor, model, existing)
        default = (product or sibling.get('product') or
                   (supported[0] if supported else DEFAULT_PRODUCT))

        plan = {
            'library_id': int(library_id),
            'library': f'{vendor} {model}'.strip(),
            'drives_now': len(existing), 'max_drives': layout.max_drives,
            'layout': layout.title,
            'full': len(existing) >= layout.max_drives,
            'slot': slot,
            'serial': f"{library.get('serial', 'XYZZY')}D{slot}",
            'vendor': sibling.get('vendor') or library.get('drive_vendor') or DEFAULT_VENDOR,
            'product': default,
            'supported': supported,
        }
        try:
            plan['drive_id'] = ids.drive_ids(conf, int(library_id), [slot])[0]
        except ids.OutOfIds:
            plan['drive_id'] = None
        plan['target'] = self._next_free_target(config)

        if plan['full']:
            refused = failure_result(
                f'Library {library_id} is full: {layout.title} holds at most '
                f'{layout.max_drives} drives', ['remove a drive first'],
                operation_id)
            # The page still shows what the library is and what it holds.
            refused.data = plan
            return refused
        return success_result(
            f'Drive {plan["drive_id"]} would go in slot {slot}', plan, operation_id)

    @staticmethod
    def _next_free_slot(existing: Dict) -> int:
        used = {data.get('slot') for data in existing.values() if data.get('slot')}
        slot = 1
        while slot in used:
            slot += 1
        return slot

    @staticmethod
    def _supported_drives(vendor: str, model: str) -> List[str]:
        """The drive models this library model takes, from its vendor profile.

        The same cascade the create form applies. An unknown profile - a
        library made by hand, or one MHVTL knows and the profiles do not -
        returns nothing, and the caller then offers what MHVTL recognises.
        """
        try:
            return list(profiles_data.get_valid_drives_for_library(vendor.upper(), model))
        except (KeyError, ValueError):
            return []

    @classmethod
    def _allowed_drives(cls, vendor: str, model: str, existing: Dict) -> List[str]:
        """What may be added: the profile's list, plus whatever is already in
        the library.

        Libraries on real hosts do not always match their profile - library 10
        here is an STK L700 holding IBM LTO-8 drives - and refusing to add a
        drive identical to the four already in it would be no help to anyone.
        """
        allowed = cls._supported_drives(vendor, model)
        for data in sorted(existing.values(), key=lambda d: d.get('slot') or 0):
            product = data.get('product')
            if product and product not in allowed:
                allowed.insert(0, product)
        return allowed

    def add(self, library_id: int, drive_data: Dict = None, *,
            restart: bool = True) -> ServiceResult:
        """Add a drive to an existing library.

        The drive id comes from config/ids: library_id + slot while that is free
        and not a multiple of ten, otherwise the next free id - MHVTL ties a
        drive to its library by the Library ID line, not by the id. This used to
        take library_id + slot unconditionally and check it only against other
        drives, so a tenth drive on library 10 got id 20, library 20's id.

        The SCSI target is the lowest free one: device.conf addresses need not be
        contiguous, and reusing a taken target would give two devices the same
        address.

        The drive model defaults to the model the library's drives already are,
        and the count is checked against the element layout of the library
        model MHVTL emulates (profiles/personalities), because MHVTL ignores a
        drive past that limit rather than reporting it.
        """
        operation_id = str(uuid.uuid4())[:8]
        drive_data = drive_data or {}

        try:
            with FileLock(lock_path(self.config_dir)):
                config = self._parse_config()

                library = config.get('libraries', {}).get(library_id)
                if library is None:
                    return failure_result(
                        f'Library {library_id} not found',
                        ['Cannot add a drive to a library that does not exist'],
                        operation_id)

                existing = {drive_id: data
                            for drive_id, data in config.get('drives', {}).items()
                            if data.get('library_id') == library_id}

                slot = self._next_free_slot(existing)

                current = shell.sudo_cat(self.device_conf)
                if not current.ok:
                    return failure_result(f'Could not read {self.device_conf}',
                                          [current.stderr.strip()], operation_id)
                conf = device_conf_format.parse(current.stdout)

                layout = personalities.library_layout(library.get('vendor', ''),
                                                      library.get('product', ''))
                if len(existing) + 1 > layout.max_drives:
                    return failure_result(
                        f'Library {library_id} is full: {layout.title} holds at '
                        f'most {layout.max_drives} drives',
                        [f'MHVTL would ignore the extra drive ({layout.source})'],
                        operation_id)

                try:
                    drive_id = ids.drive_ids(conf, library_id, [slot])[0]
                except ids.OutOfIds as exc:
                    return failure_result('No drive id is free', [str(exc)],
                                          operation_id)

                # Match the drives already in the library unless told otherwise.
                sibling = next(iter(sorted(existing.items())), (None, {}))[1]
                vendor = drive_data.get('vendor') or sibling.get('vendor') or DEFAULT_VENDOR
                product = drive_data.get('product') or sibling.get('product') or DEFAULT_PRODUCT
                if personalities.drive_personality(product) == personalities.GENERIC_DRIVE:
                    return failure_result(
                        f'MHVTL does not recognise the drive model {product!r}',
                        ['it would be emulated as a generic drive '
                         '(usr/cmd/vtltape.c tape_drives[])'], operation_id)

                # The library's own profile decides which drives it takes; the
                # create form has always applied this cascade and adding a
                # drive afterwards did not, so a library could end up with a
                # drive its model never had.
                allowed = self._allowed_drives(library.get('vendor', ''),
                                               library.get('product', ''), existing)
                if allowed and product not in allowed:
                    return failure_result(
                        f'{library.get("vendor", "")} {library.get("product", "")} '
                        f'does not take a {product}',
                        [f'it takes: {", ".join(allowed)}'], operation_id)

                target = self._next_free_target(config)
                if target is None:
                    return failure_result(
                        'No free SCSI target left in device.conf',
                        [f'targets 0-{device_conf_format.MAX_TARGET} are all '
                         f'in use'], operation_id)

                entry = device_conf_format.render_drive(
                    drive_id=drive_id, library_id=library_id, slot=slot, target=target,
                    vendor=vendor,
                    product=product,
                    serial=drive_data.get('serial')
                            or f"{library.get('serial', 'XYZZY')}D{slot}")

                text = current.stdout.rstrip('\n') + '\n\n' + entry
                written = self._write_device_conf(text)
                if not written.success:
                    return written

                contents_updated = self._add_to_library_contents(library_id, slot)

            message = f'Drive {drive_id} added to library {library_id}'
            if not contents_updated:
                message += ' (library_contents was not updated; check it by hand)'
            note, applied = (self._apply(library_id, start=drive_id) if restart
                             else ('', False))
            message += note

            return success_result(message, {
                'drive_id': drive_id, 'library_id': library_id, 'slot': slot,
                'target': target, 'restart_required': not applied,
                'restarted': applied,
                'library_contents_updated': contents_updated,
            }, operation_id)

        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('adding a drive to library %s', library_id)
            return failure_result(f'Error adding drive: {exc}', [str(exc)], operation_id)

    def remove(self, drive_id: int, *, restart: bool = True) -> ServiceResult:
        """Remove a drive from device.conf and from its library_contents."""
        operation_id = str(uuid.uuid4())[:8]

        try:
            with FileLock(lock_path(self.config_dir)):
                config = self._parse_config()
                data = config.get('drives', {}).get(drive_id)
                if data is None:
                    return failure_result(f'Drive {drive_id} not found',
                                          [f'No drive with id {drive_id}'], operation_id)

                library_id, slot = data.get('library_id'), data.get('slot')

                current = shell.sudo_cat(self.device_conf)
                if not current.ok:
                    return failure_result(f'Could not read {self.device_conf}',
                                          [current.stderr.strip()], operation_id)

                text = device_conf_format.remove_record(
                    current.stdout, 'Drive', drive_id)
                if text is None:
                    return failure_result(
                        f'Drive {drive_id} is in the parsed config but its entry could '
                        f'not be located in {self.device_conf}',
                        ['Refusing to rewrite the file rather than risk removing the '
                         'wrong entry'], operation_id)

                written = self._write_device_conf(text)
                if not written.success:
                    return written

                if library_id is not None and slot is not None:
                    self._remove_from_library_contents(library_id, slot)

            note, applied = (self._apply(library_id, stop=drive_id)
                             if restart and library_id is not None else ('', False))
            return success_result(
                f'Drive {drive_id} removed from library {library_id}{note}',
                {'drive_id': drive_id, 'library_id': library_id, 'slot': slot,
                 'restart_required': not applied, 'restarted': applied},
                operation_id)

        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('removing drive %s', drive_id)
            return failure_result(f'Error removing drive: {exc}', [str(exc)], operation_id)

    def update(self, drive_id: int, drive_data: Dict) -> ServiceResult:
        """Change a drive's vendor, product or serial.

        Only the identification fields: changing a drive's SCSI address or its
        library means removing it and adding it again, because both affect the id
        and the device mapping.
        """
        operation_id = str(uuid.uuid4())[:8]
        editable = {'vendor': 'Vendor identification',
                    'product': 'Product identification',
                    'serial': 'Unit serial number'}
        changes = {key: value for key, value in (drive_data or {}).items()
                   if key in editable and value}

        if not changes:
            return failure_result(
                'Nothing to update',
                [f'Editable fields are: {", ".join(sorted(editable))}'], operation_id)

        try:
            with FileLock(lock_path(self.config_dir)):
                config = self._parse_config()
                if drive_id not in config.get('drives', {}):
                    return failure_result(f'Drive {drive_id} not found',
                                          [f'No drive with id {drive_id}'], operation_id)

                current = shell.sudo_cat(self.device_conf)
                if not current.ok:
                    return failure_result(f'Could not read {self.device_conf}',
                                          [current.stderr.strip()], operation_id)

                text = current.stdout
                for field, label in editable.items():
                    if field in changes:
                        text = device_conf_format.replace_field(
                            text, 'Drive', drive_id, label, changes[field])

                written = self._write_device_conf(text)
                if not written.success:
                    return written

            return success_result(
                f'Drive {drive_id} updated: {", ".join(sorted(changes))}',
                {'drive_id': drive_id, 'changed': changes, 'restart_required': True},
                operation_id)

        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('updating drive %s', drive_id)
            return failure_result(f'Error updating drive: {exc}', [str(exc)], operation_id)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _next_free_target(config: Dict) -> Optional[int]:
        """Lowest SCSI target not claimed by a library or a drive."""
        used = {entry.get('target') for entry in config.get('libraries', {}).values()}
        used |= {entry.get('target') for entry in config.get('drives', {}).values()}
        used.discard(None)
        return next((t for t in range(device_conf_format.MAX_TARGET + 1)
                     if t not in used), None)

    def _write_device_conf(self, text: str) -> ServiceResult:
        """Write device.conf atomically, falling back to sudo tee when root owns it."""
        try:
            atomic_write_text(self.device_conf, text)
            return success_result('device.conf written')
        except PermissionError:
            result = shell.sudo_tee(self.device_conf, text)
            if result.ok:
                return success_result('device.conf written through sudo')
            return failure_result(f'Could not write {self.device_conf}',
                                  [result.stderr.strip()])
        except OSError as exc:
            return failure_result(f'Could not write {self.device_conf}', [str(exc)])

    def _library_contents_lines(self, library_id: int) -> Optional[List[str]]:
        path = library_contents_path(library_id, self.config_dir)
        result = shell.sudo_cat(path)
        if not result.ok:
            logger.warning('cannot read %s: %s', path, result.stderr.strip())
            return None
        return result.stdout.splitlines(keepends=True)

    def _write_library_contents(self, library_id: int, lines: List[str]) -> bool:
        path = library_contents_path(library_id, self.config_dir)
        text = ''.join(lines)
        try:
            atomic_write_text(path, text)
            return True
        except PermissionError:
            return shell.sudo_tee(path, text).ok
        except OSError as exc:
            logger.warning('cannot write %s: %s', path, exc)
            return False

    def _add_to_library_contents(self, library_id: int, slot: int) -> bool:
        """Append `Drive N:` after the last existing Drive line.

        The drive lines are a block at the top of the file; a new drive goes at
        the end of that block, not at the end of the file.
        """
        lines = self._library_contents_lines(library_id)
        if lines is None:
            return False
        if any(re.match(rf'^\s*Drive\s+{slot}\s*:', line) for line in lines):
            return True                                    # already there

        text = library_contents_format.add_drive_slot(''.join(lines), slot)
        return self._write_library_contents(library_id, text.splitlines(keepends=True))

    def _remove_from_library_contents(self, library_id: int, slot: int) -> bool:
        lines = self._library_contents_lines(library_id)
        if lines is None:
            return False
        text = library_contents_format.remove_drive_slot(''.join(lines), slot)
        if text == ''.join(lines):
            return True                                    # nothing to remove
        return self._write_library_contents(library_id, text.splitlines(keepends=True))
