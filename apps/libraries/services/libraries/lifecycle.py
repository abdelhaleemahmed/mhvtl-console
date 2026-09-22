"""Creating and deleting a library: the paths that write device.conf.

Moved from mhvtl_library_service.py:create_library (171 lines) and
delete_library (93), with the helpers they called. These two change the
configuration every daemon reads, so what they do is worth stating plainly.

Creating a library, in order:

    1. fill in what the vendor profile decides      spec.apply_defaults
    2. validate the finished specification          validation.validate
    3. back up the configuration                    config.ConfigService.backup
    4. write device.conf                            config.device_conf
    5. write library_contents.<id>                  config.library_contents
    6. enable and start the systemd units           console.units

Deleting reverses it: stop the units first, then remove the configuration they
read. A daemon whose device.conf entry has vanished logs errors until it is
killed, and a library_contents file removed under a running robot is worse.

Two rules hold throughout:

    The backup is taken before anything is written, and the write is refused if
    the backup fails. The alternative is a half-written device.conf and no copy
    of what was there before - which is how 72 tapes were lost once already.

    Any failure after the first write restores the backup. A create that fails
    between device.conf and library_contents leaves a library the daemons will
    start and the robot cannot use.

One duplication removed on the way. create_library repeated three profile
compatibility checks inline - media supported by the library, drive compatible
with the library, media supported by the drive - and then called
validate_library, which checks the same three and more. The inline copies are
gone; validation.validate is the complete version.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import device_conf as device_conf_format
from ..config import ids
from ..config import library_contents as library_contents_format
from ..config.service import ConfigService
from ..console import units
from ..profiles import personalities
from ..core import (FileLock, ServiceResult, config_dir, failure_result,
                    library_contents_path, lock_path, shell, success_result)
from . import spec as spec_rules
from . import validation

logger = logging.getLogger(__name__)


def daemons_are_ours(directory) -> bool:
    """Do the running daemons read this configuration directory?

    They read the live one. An operation writing somewhere else - a test, a
    scratch copy, a restore staged elsewhere - must not start or stop them:
    it would apply a file they never read, and stop libraries that are in use.
    """
    return Path(directory).resolve() == Path(config_dir()).resolve()


def allocate_targets(device_text: str, num_drives: int) -> Tuple[int, List[int]]:
    """One SCSI target for the library, then one per drive.

    Contiguous above the highest target in use when that fits under
    MAX_TARGET, otherwise the first free run, otherwise any free targets -
    config/ids.targets says why in each case. Raises ids.OutOfIds when fewer are
    free than needed.
    """
    chosen = ids.targets(device_conf_format.parse(device_text or ''),
                         int(num_drives) + 1)
    return chosen[0], list(chosen[1:])


def create(library_spec: Dict[str, Any], config_directory=None,
           next_id: Optional[Callable[[], int]] = None,
           start_services: bool = True) -> ServiceResult:
    """Create a library, its drives and its media slots.

    Args:
        library_spec: at minimum a profile key; see spec.apply_defaults for what
            is filled in and what may be overridden.
        config_directory: which /etc/mhvtl to write. Always passed explicitly by
            tests - a service that defaults to the live directory once created a
            library on the running host.
        next_id: called to allocate an id when the specification has none.
        start_services: enable and start the units. False leaves the
            configuration written and the daemons down, which is what an
            operator creating several libraries at once wants.

    Returns a failure, not an exception, for anything an operator can fix: an
    unknown profile, an id already in use, media the drive cannot read.
    """
    operation_id = str(uuid.uuid4())[:8]
    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)

    with FileLock(lock_path(directory)):
        # 1. defaults from the vendor profile
        try:
            filled = spec_rules.apply_defaults(library_spec, next_id=next_id)
        except spec_rules.UnknownProfile as exc:
            return failure_result(f'Invalid library specification: {exc}',
                                  [str(exc)], operation_id)

        library_id = int(filled['library_id'])
        num_drives = int(filled['num_drives'])

        # 2. validate the finished specification, profile rules and all
        checked = validation.validate(filled, directory)
        if not checked.is_valid:
            return failure_result('Library validation failed',
                                  list(checked.errors) + list(checked.suggested_fixes),
                                  operation_id)

        # Plan every id and target before anything is written, so a host that
        # has run out fails here rather than after the backup.
        existing = config.device_conf_text() or ''
        try:
            library_target, drive_ids, drive_targets = ids.plan_library(
                device_conf_format.parse(existing), library_id, num_drives)
        except ids.OutOfIds as exc:
            return failure_result(f'Library {library_id} cannot be created: {exc}',
                                  [str(exc)], operation_id)

        # 3. back up before writing anything
        backup = config.backup()
        backup_path = backup.data.get('path') if backup.success else None
        if existing.strip() and not backup.success:
            return failure_result(
                'Refusing to write device.conf: the backup failed',
                backup.errors, operation_id)

        try:
            # 4. device.conf: library block, then one block per drive
            filled['target'] = library_target
            appended = device_conf_format.render_library_and_drives(
                existing, filled, drive_targets, drive_ids=drive_ids)

            # A host creating its first library has no device.conf; it gets
            # MHVTL's own header, which declares the file VERSION the daemons
            # parse for.
            header = device_conf_format.header_if_empty(existing)
            separator = '\n' if existing and not existing.endswith('\n') else ''
            written = config.write_device_conf(
                header + existing + separator + appended, backup=False)
            if not written.success:
                return written

            # 5. library_contents.<id>
            contents = library_contents_format.render_new(
                library_id, num_drives,
                barcode_prefix=filled.get('barcode_prefix'),
                media_suffix=filled.get('media_suffix'),
                media_count=int(filled.get('media_count', 39)),
                empty_slots=int(filled.get('empty_slots', 0)),
                map_count=int(filled.get('map_count', 4)))

            contents_written = config.write_library_contents(library_id, contents,
                                                             backup=False)
            if not contents_written.success:
                raise RuntimeError(
                    f'library_contents.{library_id} could not be written: '
                    + '; '.join(contents_written.errors))

        except Exception as exc:                       # noqa: BLE001 - rolled back
            logger.error('creating library %s failed, restoring: %s', library_id, exc)
            _roll_back(config, backup_path, library_id, directory)
            return failure_result(
                f'Library {library_id} was not created: {exc}',
                [str(exc), 'the previous configuration has been restored'
                 if backup_path else 'there was no backup to restore'],
                operation_id)

    # 6. start the daemons. Outside the lock, and best effort: the configuration
    # is written and correct, so a unit that will not start is a fact to report,
    # not a reason to undo the library.
    started = {}
    if start_services and daemons_are_ours(directory):
        started = units.start_library(library_id, drive_ids)

    failed_units = [unit for unit, ok in started.items() if not ok]
    message = (f'Library {library_id} created: {filled.get("vendor")} '
               f'{filled.get("product")} with {num_drives} drives')
    if failed_units:
        message += f' ({len(failed_units)} unit(s) did not start)'

    return success_result(message, {
        'library_id': library_id,
        'channel': int(filled['channel']),
        'target': library_target,
        'lun': int(filled['lun']),
        'drive_targets': drive_targets,
        'drive_ids': drive_ids,
        'library_contents_file': str(library_contents_path(library_id, directory)),
        'backup_path': backup_path,
        'barcode_prefix': filled.get('barcode_prefix'),
        'media_suffix': filled.get('media_suffix'),
        'media_count': int(filled.get('media_count', 39)),
        'units_failed': failed_units,
    }, operation_id)


def preview(library_spec: Dict[str, Any], config_directory=None) -> ServiceResult:
    """The device.conf text create() would append, without writing anything.

    The same defaults, validation and id and target planning as create(), so
    the create form shows the ids and addresses the library would really get.
    The adapter this replaces allocated targets but not drive ids, so its
    preview could show drive ids create() would not use.

    Validation problems do not stop the preview: they are returned beside the
    text, because the form shows both. Errors and warnings stay apart -
    an error is what create() would refuse, a warning is what it would accept
    and grumble about - and a caller that wants them together can add the two
    lists.
    """
    operation_id = str(uuid.uuid4())[:8]
    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)
    try:
        filled = spec_rules.apply_defaults(library_spec)
    except spec_rules.UnknownProfile as exc:
        return failure_result(f'Cannot preview: {exc}', [str(exc)], operation_id)

    existing = config.device_conf_text() or ''
    try:
        library_target, drive_ids, drive_targets = ids.plan_library(
            device_conf_format.parse(existing), int(filled['library_id']),
            int(filled['num_drives']))
    except ids.OutOfIds as exc:
        return failure_result(f'Cannot preview: {exc}', [str(exc)], operation_id)

    filled['target'] = library_target
    text = device_conf_format.render_library_and_drives(
        existing, filled, drive_targets, drive_ids=drive_ids)
    checked = validation.validate(filled, directory)
    return success_result(
        f'Preview of library {filled["library_id"]}',
        {'text': text, 'library_id': int(filled['library_id']),
         'drive_ids': list(drive_ids), 'errors': list(checked.errors),
         'warnings': list(checked.warnings)}, operation_id)


def _roll_back(config: ConfigService, backup_path, library_id: int,
               directory: Path) -> None:
    """Undo a partial create: restore the backup, drop the contents file."""
    if backup_path:
        restored = config.restore(backup_path)
        if not restored.success:
            logger.error('rollback failed for library %s: %s', library_id,
                         '; '.join(restored.errors))

    path = library_contents_path(library_id, directory)
    try:
        if path.exists():
            path.unlink()
    except PermissionError:
        shell.sudo(['rm', '-f', str(path)])
    except OSError as exc:
        logger.warning('could not remove %s during rollback: %s', path, exc)


def check_safe_to_delete(library_id: int,
                         config_directory=None) -> Tuple[bool, str, List[Dict]]:
    """Is anything loaded in this library's drives?

    Deleting a library with a tape in a drive loses whatever the drive has not
    yet flushed, and leaves a media file that no library claims. An offline
    library is safe - there is nothing running to lose - so a status call that
    fails is not by itself a refusal: it usually means the daemons are down,
    which is the state a delete is heading for anyway.
    """
    from ..operations.service import OperationsService

    try:
        status = OperationsService(config_directory).status(library_id)
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.warning('could not check library %s before deleting: %s',
                       library_id, exc)
        return True, f'Could not verify status: {exc}', []

    if not status.success:
        return True, 'Library is offline or its status could not be read', []

    loaded = [drive for drive in (status.data or {}).get('drives', [])
              if drive.get('full')]
    if loaded:
        described = ', '.join(
            f'drive {drive.get("number")} ({drive.get("barcode") or "unknown tape"})'
            for drive in loaded)
        return False, (f'{len(loaded)} drive(s) still have tapes loaded: '
                       f'{described}'), loaded

    return True, 'No tapes are loaded in this library', []


def delete(library_id: int, *, force: bool = False, remove_media: bool = False,
           config_directory=None) -> ServiceResult:
    """Remove a library, its drives, its contents file and optionally its media.

    Media is kept unless asked for: the tapes under a library's barcodes are
    data, and a library definition is cheap to recreate. force skips the
    loaded-tape check, which is the operator saying they know.
    """
    operation_id = str(uuid.uuid4())[:8]
    library_id = int(library_id)
    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)

    with FileLock(lock_path(directory)):
        text = config.device_conf_text()
        if text is None:
            return failure_result('device.conf could not be read',
                                  [str(directory / 'device.conf')], operation_id)

        parsed = device_conf_format.parse(text)
        if library_id not in parsed.libraries:
            return failure_result(
                f'Library {library_id} does not exist',
                [f'no `Library: {library_id}` entry in device.conf'], operation_id)

        if not force:
            safe, reason, loaded = check_safe_to_delete(library_id, directory)
            if not safe:
                # The caller needs requires_force to offer the override, so this
                # one failure is built directly rather than by failure_result.
                return ServiceResult(
                    success=False, message=reason, errors=[reason],
                    data={'loaded_drives': loaded, 'requires_force': True},
                    operation_id=operation_id)

        drive_ids = sorted(parsed.drives_of(library_id))

        # Barcodes come from the contents file, which is about to be removed.
        barcodes = []
        if remove_media:
            contents = config.library_contents(library_id)
            barcodes = list(contents.barcodes) if contents else []

        backup = config.backup()
        if not backup.success:
            return failure_result('Refusing to change device.conf: the backup failed',
                                  backup.errors, operation_id)
        backup_path = backup.data.get('path')

        # Stop the daemons before the configuration they read goes away.
        touch_daemons = daemons_are_ours(directory)
        if touch_daemons:
            units.stop_library(library_id, drive_ids)

        try:
            remaining = device_conf_format.remove_record(text, 'Library', library_id)
            if remaining is None:
                raise RuntimeError(
                    f'library {library_id} is in the parsed configuration but its '
                    f'record could not be located in device.conf')

            for drive_id in drive_ids:
                without_drive = device_conf_format.remove_record(
                    remaining, 'Drive', drive_id)
                if without_drive is None:
                    logger.warning('drive %s could not be removed from device.conf',
                                   drive_id)
                else:
                    remaining = without_drive

            written = config.write_device_conf(remaining, backup=False)
            if not written.success:
                raise RuntimeError('; '.join(written.errors))
            if touch_daemons:
                # Only now is the library gone from device.conf; see
                # units.forget_library for why the earlier reload is not enough.
                units.forget_library(library_id, drive_ids)

        except Exception as exc:                       # noqa: BLE001 - rolled back
            logger.error('deleting library %s failed, restoring: %s', library_id, exc)
            if backup_path:
                config.restore(backup_path)
            if touch_daemons:
                units.start_library(library_id, drive_ids)
            return failure_result(f'Library {library_id} was not deleted: {exc}',
                                  [str(exc), 'the configuration has been restored'],
                                  operation_id)

        path = library_contents_path(library_id, directory)
        try:
            if path.exists():
                path.unlink()
        except PermissionError:
            shell.sudo(['rm', '-f', str(path)])
        except OSError as exc:
            logger.warning('could not remove %s: %s', path, exc)

    media_removed, media_failed = _remove_media(barcodes) if barcodes else (0, [])

    detail = ''
    if remove_media:
        detail = f' ({media_removed} media file set(s) removed'
        detail += f', {len(media_failed)} failed)' if media_failed else ')'

    return success_result(f'Library {library_id} deleted{detail}', {
        'library_id': library_id,
        'drive_ids': drive_ids,
        'media_removed': media_removed,
        'media_failed': media_failed,
        'backup_path': backup_path,
    }, operation_id)


def _remove_media(barcodes: List[str]) -> Tuple[int, List[str]]:
    """Delete each barcode's media directory. Reports what it could not remove.

    Deliberately one barcode at a time through tapes.media, which refuses a
    barcode that escapes the media directory. The version this replaces built a
    path by string concatenation and handed it to `sudo rm -rf`.
    """
    from ..tapes import media

    removed, failed = 0, []
    for barcode in barcodes:
        try:
            if media.delete(barcode).ok:
                removed += 1
            else:
                failed.append(barcode)
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.warning('could not remove media %s: %s', barcode, exc)
            failed.append(barcode)
    return removed, failed


def update(library_id: int, library_spec: Dict[str, Any],
           config_directory=None) -> ServiceResult:
    """Change a library, by deleting it and creating it again.

    Moved from mhvtl_library_service.py:update_library. MHVTL has no way to
    change a library in place - vendor, product and drive count are all read
    once at daemon start - so an update is a delete followed by a create, under
    one lock, with one backup around both.

    What that costs the operator, stated because the old version did not:
    library_contents is regenerated, so slot assignments and any hand-edited
    barcodes are replaced by a fresh series. Media files are kept, but a tape
    whose barcode is no longer in the new series is no longer in the library.

    force is not offered. An update that stops the daemons while a tape is
    loaded is the case the loaded-drive check exists for, and an operator who
    means it can delete with force and create.
    """
    operation_id = str(uuid.uuid4())[:8]
    library_id = int(library_id)
    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)

    with FileLock(lock_path(directory)):
        backup = config.backup()
        if not backup.success:
            return failure_result('Refusing to update: the backup failed',
                                  backup.errors, operation_id)
        backup_path = backup.data.get('path')

        removed = delete(library_id, config_directory=directory)
        if not removed.success:
            return failure_result(
                f'Library {library_id} was not updated: {removed.message}',
                removed.errors or [removed.message], operation_id)

        spec_with_id = dict(library_spec or {})
        spec_with_id['library_id'] = library_id

        created = create(spec_with_id, directory)
        if not created.success:
            logger.error('recreating library %s failed, restoring: %s',
                         library_id, created.message)
            if backup_path:
                config.restore(backup_path)
            return failure_result(
                f'Library {library_id} was deleted but could not be recreated: '
                f'{created.message}',
                created.errors + ['the previous configuration has been restored'],
                operation_id)

    return success_result(
        f'Library {library_id} updated (deleted and recreated)',
        {**(created.data or {}), 'backup_path': backup_path,
         'library_contents_regenerated': True}, operation_id)


def recognised(library_id: int, config_directory=None) -> bool:
    """Has the running system actually picked this library up?

    Three things have to agree: device.conf declares it, its contents file
    exists, and its robot daemon is active. Any one of them alone is the state
    after a half-finished create.
    """
    library_id = int(library_id)
    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)

    text = config.device_conf_text()
    if text is None or library_id not in device_conf_format.parse(text).libraries:
        return False
    if config.library_contents_text(library_id) is None:
        return False

    return units.is_active(f'vtllibrary@{library_id}.service')


def set_empty_slots(library_id: int, empty: int, config_directory=None) -> ServiceResult:
    """Change how many empty slots a library has, keeping its tapes.

    Slots live in library_contents, which the vtllibrary daemon reads once at
    start, so this rewrites the file and the caller restarts the library -
    nothing here touches device.conf or any media. The tapes stay in the slot
    numbers they are in: renumbering them would move a tape the robot has
    already reported at a given address.

    Growing appends empty slots after the last one. Shrinking removes empty
    slots from the end and stops at the first tape, because MHVTL stops reading
    at the first missing slot number - a gap would silently shorten the
    library.
    """
    operation_id = str(uuid.uuid4())[:8]
    if int(empty) < 0:
        return failure_result('Empty slots cannot be negative',
                              ['pass 0 or more'], operation_id)

    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)
    contents = config.library_contents(library_id)
    if contents is None:
        return failure_result(
            f'Could not read library {library_id}',
            [f'library_contents.{library_id} is not readable'], operation_id)

    slots = sorted(contents.slots, key=lambda slot: slot.number)
    occupied = [slot for slot in slots if slot.full]
    current_empty = len(slots) - len(occupied)
    wanted = int(empty)

    conf_text = config.device_conf_text() or ''
    library = device_conf_format.parse(conf_text).libraries.get(int(library_id), {})
    problems = personalities.limits_problems(
        library.get('vendor', ''), library.get('product', ''),
        drives=contents.drive_count, slots=len(occupied) + wanted,
        maps=len(contents.map_slots))
    if problems:
        return failure_result(f'Library {library_id} cannot have '
                              f'{len(occupied) + wanted} slots',
                              problems, operation_id)

    if wanted > current_empty:
        next_number = (max((slot.number for slot in slots), default=0) + 1)
        for _ in range(wanted - current_empty):
            slots.append(library_contents_format.Slot(next_number))
            next_number += 1
    elif wanted < current_empty:
        removable = 0
        for slot in reversed(slots):
            if slot.full:
                break
            removable += 1
        if current_empty - wanted > removable:
            return failure_result(
                f'Library {library_id} cannot go down to {wanted} empty slots',
                [f'only {removable} empty slot(s) come after the last tape, and '
                 'a gap in the middle would shorten the library',
                 'move or remove the tapes at the end first'], operation_id)
        del slots[len(slots) - (current_empty - wanted):]

    contents.slots = slots
    written = config.write_library_contents(
        library_id, library_contents_format.render(contents))
    if not written.success:
        return written

    total, tapes = len(slots), len(occupied)
    return success_result(
        f'Library {library_id} now has {total} slots: {tapes} with tapes, '
        f'{total - tapes} empty',
        {'library_id': int(library_id), 'total_slots': total,
         'full_slots': tapes, 'empty_slots': total - tapes,
         'was_empty_slots': current_empty, 'restart_required': True},
        operation_id)
