"""Leftovers: things one part of the system believes in and another does not.

Moved from mhvtl_library_service.py:find_orphans and cleanup_orphans (232 lines
between them), which until now this module only delegated to.

Three kinds, all produced by a delete that stopped half way:

    orphaned drives     a `Drive:` record whose `Library ID:` names a library
                        device.conf no longer declares. The daemon starts, the
                        robot it reports to does not exist.
    orphaned files      library_contents.N with no `Library: N`. Harmless until
                        an id is reused, when the new library inherits the old
                        one's slots.
    orphaned units      vtllibrary@N or vtltape@N that systemd still knows about
                        with no N in device.conf. A template instance stays
                        loaded until the machine reboots, so these accumulate.
    orphaned media      a tape's files under the media directory that no
                        library_contents lists - what `tape delete` without
                        --remove-media leaves. The data is intact and can be
                        given back to a library (tapes.adopt), which is why
                        these are reported but never cleaned.

What cleanup does *not* do is remove media. A stale database row is not a good
enough reason to delete tapes; lifecycle.delete(remove_media=True) is where that
decision is made, by someone who asked for it.

Order matters in cleanup, for the same reason it does in lifecycle.delete: stop
the daemons before removing what they read.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ..config import device_conf as device_conf_format
from ..config import library_contents as library_contents_format
from ..config.service import ConfigService
from ..console import units
from ..core import (FileLock, ServiceResult, config_dir, failure_result,
                    lock_path, shell, success_result)

logger = logging.getLogger(__name__)

#: library_contents.10 -> 10. Anything else in the directory is not ours.
CONTENTS_RE = re.compile(r'^library_contents\.(\d+)$')


def find(config_directory=None) -> Dict[str, Any]:
    """What device.conf, the config directory and systemd disagree about.

    Returns the report even when parts of it could not be gathered - a host
    without systemd should still get the file and drive answers - so every
    caller checks the lists rather than a success flag.
    """
    directory = Path(config_directory) if config_directory else config_dir()
    report = {
        'orphaned_drives': [],
        'orphaned_files': [],
        'orphaned_services': [],
        'orphaned_media': [],
        'valid_library_ids': [],
        'valid_drive_ids': [],
        'unreadable': [],
    }

    text = ConfigService(directory).device_conf_text()
    if text is None:
        # Without device.conf nothing can be called an orphan: every library
        # would look orphaned, and cleanup would delete the lot.
        report['unreadable'].append(str(directory / 'device.conf'))
        return report

    conf = device_conf_format.parse(text)
    libraries, drives = set(conf.libraries), set(conf.drives)
    report['valid_library_ids'] = sorted(libraries)
    report['valid_drive_ids'] = sorted(drives)

    for drive_id, drive in conf.drives.items():
        library_id = drive.get('library_id')
        if library_id is not None and library_id not in libraries:
            report['orphaned_drives'].append({
                'drive_id': drive_id, 'library_id': library_id,
                'reason': f'references library {library_id}, which is not declared'})

    try:
        for entry in sorted(directory.iterdir()):
            match = CONTENTS_RE.match(entry.name)
            if match and int(match.group(1)) not in libraries:
                library_id = int(match.group(1))
                report['orphaned_files'].append({
                    'path': str(entry), 'library_id': library_id,
                    'reason': f'no `Library: {library_id}` in device.conf'})
    except OSError as exc:
        logger.warning('scanning %s for orphaned files: %s', directory, exc)
        report['unreadable'].append(str(directory))

    # Tapes on disk that no library claims. media/ knows the directory; the
    # barcodes come from every library_contents in the configuration, not only
    # the declared libraries, so a tape in a library that is merely stopped is
    # not called an orphan.
    try:
        from ..tapes import media as tape_media
        claimed = set()
        for entry in sorted(directory.iterdir()):
            if CONTENTS_RE.match(entry.name):
                claimed.update(library_contents_format.parse(
                    entry.read_text(errors='replace')).barcodes)
        for barcode in tape_media.list_media():
            if barcode not in claimed:
                report['orphaned_media'].append({
                    'barcode': barcode,
                    'path': str(tape_media.path_for(barcode)),
                    'reason': 'no library_contents lists this barcode'})
    except OSError as exc:
        logger.warning('scanning the media directory: %s', exc)
        report['unreadable'].append('media directory')

    for unit in units.all_vtl_units():
        instance = units.instance_id(unit)
        if instance is None:
            continue
        is_library = unit.startswith('vtllibrary@')
        known = libraries if is_library else drives
        if instance not in known:
            report['orphaned_services'].append({
                'service': unit, 'type': 'library' if is_library else 'drive',
                'id': instance,
                'reason': f'no {"Library" if is_library else "Drive"} {instance} '
                          f'in device.conf'})

    return report


def find_result(config_directory=None) -> ServiceResult:
    """find() as a ServiceResult, for the CLI and the AJAX endpoints."""
    operation_id = str(uuid.uuid4())[:8]
    try:
        report = find(config_directory)
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('looking for orphans')
        return failure_result(f'Could not check for orphans: {exc}', [str(exc)],
                              operation_id)

    total = sum(len(report[key]) for key in
                ('orphaned_drives', 'orphaned_files', 'orphaned_services'))
    if report['unreadable']:
        return failure_result(
            'Could not read the configuration, so nothing was called an orphan',
            report['unreadable'], operation_id)

    # Media is counted apart: it is data, cleanup never touches it, and a
    # count that mixed the two would invite "clean it up" on tapes.
    return success_result(
        f'{total} orphaned item(s): {len(report["orphaned_drives"])} drives, '
        f'{len(report["orphaned_files"])} files, '
        f'{len(report["orphaned_services"])} units'
        + (f'; {len(report["orphaned_media"])} tape(s) on disk no library lists'
           if report['orphaned_media'] else ''),
        report, operation_id)


def cleanup(*, dry_run: bool = False, config_directory=None) -> ServiceResult:
    """Remove what find() reported, daemons first.

    dry_run returns the same report and changes nothing, which is what the
    confirmation page shows before the operator commits.
    """
    operation_id = str(uuid.uuid4())[:8]
    directory = Path(config_directory) if config_directory else config_dir()
    config = ConfigService(directory)

    report = find(directory)
    if report['unreadable']:
        return failure_result(
            'Refusing to clean up: the configuration could not be read',
            report['unreadable'] + ['every library would look orphaned'],
            operation_id)

    counts = (len(report['orphaned_drives']), len(report['orphaned_files']),
              len(report['orphaned_services']))
    summary = (f'{counts[0]} orphaned drives, {counts[1]} orphaned files, '
               f'{counts[2]} orphaned units')

    if dry_run:
        return success_result(f'Dry run: found {summary}',
                              {'orphans': report, 'dry_run': True}, operation_id)

    if not any(counts):
        return success_result('Nothing to clean up',
                              {'orphans': report, 'cleaned': {}}, operation_id)

    cleaned = {'drives_removed': [], 'files_removed': [], 'services_stopped': [],
               'processes_killed': []}
    problems = []

    with FileLock(lock_path(directory)):
        backup = config.backup()
        if not backup.success:
            return failure_result('Refusing to clean up: the backup failed',
                                  backup.errors, operation_id)
        backup_path = backup.data.get('path')

        # 1. Units first, so nothing is reading what is about to be removed.
        # Only when these daemons are the ones this directory describes: a
        # cleanup staged against a scratch copy must not stop live libraries.
        from .lifecycle import daemons_are_ours
        if report['orphaned_services'] and not daemons_are_ours(directory):
            problems.append(f'{len(report["orphaned_services"])} orphaned unit(s) '
                            f'left alone: {directory} is not the live '
                            f'configuration directory')
            report = {**report, 'orphaned_services': []}
        for entry in report['orphaned_services']:
            unit = entry['service']
            try:
                units.stop(unit)
                cleaned['processes_killed'] += units.kill_lingering(unit)
                units.disable(unit)
                units.reset_failed(unit)
                cleaned['services_stopped'].append(unit)
            except Exception as exc:                   # noqa: BLE001 - collected
                problems.append(f'could not stop {unit}: {exc}')
        if report['orphaned_services']:
            units.daemon_reload()

        # 2. Drive records whose library is gone.
        if report['orphaned_drives']:
            try:
                text = config.device_conf_text()
                if text is None:
                    raise RuntimeError('device.conf became unreadable')

                for entry in report['orphaned_drives']:
                    without = device_conf_format.remove_record(
                        text, 'Drive', entry['drive_id'])
                    if without is None:
                        problems.append(f'drive {entry["drive_id"]} could not be '
                                        f'located in device.conf')
                    else:
                        text = without
                        cleaned['drives_removed'].append(entry['drive_id'])

                if cleaned['drives_removed']:
                    written = config.write_device_conf(text, backup=False)
                    if not written.success:
                        raise RuntimeError('; '.join(written.errors))

            except Exception as exc:                   # noqa: BLE001 - rolled back
                logger.error('cleaning up orphaned drives failed, restoring: %s', exc)
                if backup_path:
                    config.restore(backup_path)
                return failure_result(
                    f'Cleanup failed: {exc}',
                    [str(exc), 'the configuration has been restored'], operation_id)

        # 3. Contents files for libraries nobody declares.
        for entry in report['orphaned_files']:
            path = Path(entry['path'])
            try:
                if path.exists():
                    try:
                        path.unlink()
                    except PermissionError:
                        if not shell.sudo(['rm', '-f', str(path)]).ok:
                            raise
                    cleaned['files_removed'].append(str(path))
            except OSError as exc:
                problems.append(f'could not remove {path}: {exc}')

    message = (f'Cleaned {len(cleaned["drives_removed"])} drives, '
               f'{len(cleaned["files_removed"])} files, '
               f'{len(cleaned["services_stopped"])} units')
    return ServiceResult(success=True, message=message,
                         data={'cleaned': cleaned, 'orphans': report,
                               'backup_path': backup_path},
                         errors=problems, operation_id=operation_id)
