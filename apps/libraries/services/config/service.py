"""Config orchestration: read, back up, write, restore.

Pulls together the write paths that were spread across mhvtl_library_service.py
(which locked and wrote atomically), backup_mhvtl_library_service.py (which did
neither) and mhvtl_script_service.py (which used a plain open() that fails on a
root-owned file).

Every write follows the same shape: take the directory lock, back up what is
there, write a temp file, fsync, rename. A reader sees the old file or the new
one, never a half-written device.conf - the difference between a library that is
briefly invisible and one the daemons refuse to start.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core import (FileLock, ServiceResult, atomic_write_text, config_dir,
                    device_conf_path, failure_result, library_contents_path,
                    lock_path, shell, success_result)
from . import device_conf as device_conf_format
from . import inventory
from . import library_contents as library_contents_format

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = 'backups'


class ConfigService:
    """Reading and writing the MHVTL configuration.

        service = ConfigService()
        conf = service.device_conf()            # parsed
        service.write_device_conf(text)         # locked, backed up, atomic
    """

    def __init__(self, config_directory=None):
        self.config_dir = Path(config_directory) if config_directory else config_dir()

    # -- reading ----------------------------------------------------------

    def device_conf_text(self) -> Optional[str]:
        result = shell.sudo_cat(device_conf_path(self.config_dir))
        return result.stdout if result.ok else None

    def device_conf(self) -> Optional[device_conf_format.DeviceConf]:
        """Parsed device.conf, or None when it cannot be read."""
        text = self.device_conf_text()
        return device_conf_format.parse(text) if text is not None else None

    def library_contents_text(self, library_id: int) -> Optional[str]:
        result = shell.sudo_cat(library_contents_path(library_id, self.config_dir))
        return result.stdout if result.ok else None

    def library_contents(self, library_id: int):
        """Parsed library_contents.N, or None when it cannot be read."""
        text = self.library_contents_text(library_id)
        return library_contents_format.parse(text) if text is not None else None

    def files(self):
        return inventory.list_files(self.config_dir)

    def read_file(self, name: str) -> Optional[str]:
        return inventory.read_file(name, self.config_dir)

    def export_zip(self) -> bytes:
        return inventory.export_zip(self.config_dir)

    # -- writing ----------------------------------------------------------

    def backup(self) -> ServiceResult:
        """Copy every config file into a timestamped backup directory."""
        operation_id = str(uuid.uuid4())[:8]
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        target = self.config_dir / BACKUP_DIRNAME / stamp

        copied, failed = [], []
        for entry in self.files():
            text = self.read_file(entry.name)
            if text is None:
                failed.append(entry.name)
                continue
            try:
                atomic_write_text(target / entry.name, text)
                copied.append(entry.name)
            except PermissionError:
                if shell.sudo(['mkdir', '-p', target]).ok and \
                        shell.sudo_tee(target / entry.name, text).ok:
                    copied.append(entry.name)
                else:
                    failed.append(entry.name)
            except OSError as exc:
                logger.warning('backing up %s: %s', entry.name, exc)
                failed.append(entry.name)

        if not copied:
            return failure_result('Could not back up any configuration file',
                                  failed, operation_id)
        return success_result(
            f'Backed up {len(copied)} file{"s" if len(copied) != 1 else ""} to {target}',
            {'path': str(target), 'files': copied, 'failed': failed}, operation_id)

    def write_device_conf(self, text: str, *, backup: bool = True) -> ServiceResult:
        """Replace device.conf, under the lock, with a backup first."""
        return self._write(device_conf_path(self.config_dir), text, backup=backup)

    def write_library_contents(self, library_id: int, text: str, *,
                               backup: bool = True) -> ServiceResult:
        return self._write(library_contents_path(library_id, self.config_dir),
                           text, backup=backup)

    def _write(self, path: Path, text: str, *, backup: bool) -> ServiceResult:
        operation_id = str(uuid.uuid4())[:8]
        try:
            with FileLock(lock_path(self.config_dir)):
                if backup:
                    backup_result = self.backup()
                    if not backup_result.success:
                        return failure_result(
                            f'Refusing to write {path.name}: the backup failed',
                            backup_result.errors, operation_id)
                try:
                    atomic_write_text(path, text)
                except PermissionError:
                    result = shell.sudo_tee(path, text)
                    if not result.ok:
                        return failure_result(f'Could not write {path}',
                                              [result.stderr.strip()], operation_id)
            return success_result(f'{path.name} written', {'path': str(path)},
                                  operation_id)
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('writing %s', path)
            return failure_result(f'Could not write {path.name}: {exc}',
                                  [str(exc)], operation_id)

    def restore(self, backup_path) -> ServiceResult:
        """Copy a backup directory back over the live configuration.

        Used as a rollback: a create or delete that fails part way through has
        already written device.conf, and leaving it half-changed is worse than
        either outcome. Restores only the files the backup holds, so a media
        file written since is left alone.
        """
        operation_id = str(uuid.uuid4())[:8]
        source = Path(backup_path)
        if not source.is_dir():
            return failure_result(f'No backup directory at {source}',
                                  [str(source)], operation_id)

        restored, failed = [], []
        for entry in sorted(source.iterdir()):
            if not entry.is_file():
                continue
            target = self.config_dir / entry.name
            try:
                atomic_write_text(target, entry.read_text())
                restored.append(entry.name)
            except PermissionError:
                if shell.sudo_tee(target, entry.read_text()).ok:
                    restored.append(entry.name)
                else:
                    failed.append(entry.name)
            except OSError as exc:
                logger.warning('restoring %s: %s', entry.name, exc)
                failed.append(entry.name)

        if failed:
            return failure_result(
                f'Restored {len(restored)} file(s); {len(failed)} could not be '
                f'written', failed, operation_id)
        return success_result(f'Restored {len(restored)} file(s) from {source}',
                              {'path': str(source), 'files': restored}, operation_id)

    def validate(self) -> ServiceResult:
        """Is the configuration readable, parseable and self-consistent?"""
        operation_id = str(uuid.uuid4())[:8]
        problems = []

        conf = self.device_conf()
        if conf is None:
            return failure_result('device.conf cannot be read',
                                  [str(device_conf_path(self.config_dir))], operation_id)

        if not conf.libraries:
            problems.append('device.conf declares no libraries')

        for drive_id, drive in conf.drives.items():
            library_id = drive.get('library_id')
            if library_id is None:
                problems.append(f'drive {drive_id} has no Library ID line')
            elif library_id not in conf.libraries:
                problems.append(f'drive {drive_id} belongs to library {library_id}, '
                                f'which is not declared')

        addresses = {}
        for kind, entries in (('library', conf.libraries), ('drive', conf.drives)):
            for device_id, entry in entries.items():
                address = (entry.get('channel'), entry.get('target'), entry.get('lun'))
                if address in addresses:
                    problems.append(
                        f'{kind} {device_id} shares SCSI address {address} with '
                        f'{addresses[address]}')
                addresses[address] = f'{kind} {device_id}'

        for library_id in conf.libraries:
            if self.library_contents(library_id) is None:
                problems.append(f'library {library_id} has no readable '
                                f'library_contents.{library_id}')

        if problems:
            return failure_result(
                f'{len(problems)} problem{"s" if len(problems) != 1 else ""} found',
                problems, operation_id)

        return success_result(
            f'Configuration is consistent: {len(conf.libraries)} libraries, '
            f'{len(conf.drives)} drives', conf.to_dict(), operation_id)

    def regenerate_library_contents(self, *, force: bool = False) -> ServiceResult:
        """Write a fresh library_contents.N for every library in device.conf.

        Moved from mhvtl_library_service.py:generate_library_contents_files. The
        repair for a host whose contents files were lost or never written - the
        libraries are declared, the daemons start, and the robots report no
        slots.

        Refuses to overwrite an existing file unless force is set, and refuses
        as a whole rather than half way through: a partial regeneration leaves
        some libraries with their old slots and some with new barcodes, which is
        harder to reason about than either.
        """
        operation_id = str(uuid.uuid4())[:8]
        with FileLock(lock_path(self.config_dir)):
            conf = self.device_conf()
            if conf is None:
                return failure_result(
                    'device.conf cannot be read',
                    [str(device_conf_path(self.config_dir))], operation_id)

            if not force:
                existing = [library_id for library_id in sorted(conf.libraries)
                            if library_contents_path(library_id,
                                                     self.config_dir).exists()]
                if existing:
                    return failure_result(
                        f'{len(existing)} library_contents file(s) already exist',
                        [f'library_contents.{library_id} exists'
                         for library_id in existing]
                        + ['pass force to overwrite them'], operation_id)

            counts = conf.drive_counts
            written, failed = [], []
            for library_id in sorted(conf.libraries):
                text = library_contents_format.render_new(
                    library_id, int(counts.get(library_id, 0)))
                result = self.write_library_contents(library_id, text, backup=False)
                if result.success:
                    written.append(f'library_contents.{library_id}')
                else:
                    failed.append(f'library_contents.{library_id}: '
                                  + '; '.join(result.errors))

        if failed:
            return failure_result(
                f'Wrote {len(written)} file(s); {len(failed)} failed',
                failed, operation_id)
        return success_result(f'Regenerated {len(written)} library_contents file(s)',
                              {'written': written}, operation_id)
