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
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from ..core import (NORMAL, QUICK, FileLock, ServiceResult, atomic_write_text,
                    config_dir, device_conf_path, failure_result,
                    library_contents_path, lock_path, shell, success_result)
from . import device_conf as device_conf_format
from . import inventory
from . import library_contents as library_contents_format

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = 'backups'

#: How backup() names a directory, and the only shape remove_backup() and
#: prune_backups() treat as one of ours. Anything else in backups/ is listed but
#: never pruned.
BACKUP_STAMP_FORMAT = '%Y%m%d_%H%M%S'
BACKUP_STAMP_RE = re.compile(r'^\d{8}_\d{6}$')


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

    # -- backups ----------------------------------------------------------
    #
    # Every config write takes one (see _write), and until now nothing ever
    # looked at them again: 946 directories and 25 MB had collected on this host
    # between 17 and 29 September, and `config restore` took a directory name
    # the operator had to already know. Listing and removal live here, in the
    # service, so the console and the CLI are both callers.

    @property
    def backup_dir(self) -> Path:
        """Where backup() writes. One definition, so a caller stops rebuilding
        the path from a literal - the CLI's restore did, with 'backups'."""
        return self.config_dir / BACKUP_DIRNAME

    def backups(self, *, with_sizes: bool = True) -> ServiceResult:
        """Every backup, newest first.

        A backup is a directory named for the moment it was taken. Anything else
        in backups/ is reported with `recognised=False` rather than hidden: three
        files named device.conf.backup.<stamp> predate this shape, and an
        operator looking for disk space should see them.

        WHY SIZES COST A SUBPROCESS. On this host backups/ is `drw-rwSr--` root:
        mhvtl - readable and writable by the group, with NO execute bit - so the
        names can be listed without privilege and the children cannot be
        stat()ed at all. Rather than one `sudo` per backup, with_sizes runs ONE
        `find` over the tree and fills every row from it: the same shape as
        media.usage_for_all(), for the same reason.

        with_sizes=False answers from the directory names alone - no subprocess,
        nothing but name, timestamp and whether the shape is ours - which is
        enough for a caller that only wants to offer a choice.
        """
        operation_id = str(uuid.uuid4())[:8]
        root = self.backup_dir
        names = self._names()
        if names is None:
            return failure_result(
                f'Cannot read {root}',
                ['the backup directory is not readable by this account'],
                operation_id)
        if not names:
            return success_result('No backups have been taken',
                                  {'backups': [], 'count': 0, 'total_bytes': 0,
                                   'measured': False, 'path': str(root)},
                                  operation_id)

        measured = self._measure_backups(root) if with_sizes else {}
        found = [self._describe_backup(root, name, measured.get(name))
                 for name in names]
        total = sum(row['bytes'] for row in found)
        note = f', {total // 1024} KiB' if measured else ''
        return success_result(
            f'{len(found)} backup{"s" if len(found) != 1 else ""}{note}',
            {'backups': found, 'count': len(found), 'total_bytes': total,
             'measured': bool(measured), 'path': str(root)}, operation_id)

    @staticmethod
    def _measure_backups(root: Path) -> Dict[str, Dict]:
        """{name: {'bytes': n, 'files': [...]}} for every backup, in ONE call.

        Returns {} when the call fails, and the caller reports zero sizes rather
        than refusing: a listing an operator can act on beats no listing.
        """
        result = shell.sudo(['find', str(root), '-mindepth', '1', '-maxdepth', '2',
                             '-printf', '%y\\t%s\\t%P\\n'], timeout=QUICK)
        if not result.ok:
            logger.warning('measuring %s: %s', root, result.output.strip()[:200])
            return {}

        measured: Dict[str, Dict] = {}
        for line in result.stdout.splitlines():
            parts = line.split('\t')
            if len(parts) != 3:
                continue
            kind, size, relative = parts
            top, _, child = relative.partition('/')
            if not top:
                continue
            row = measured.setdefault(top, {'bytes': 0, 'files': []})
            if kind != 'f':
                continue
            try:
                row['bytes'] += int(size)
            except ValueError:
                continue
            row['files'].append(child or top)
        for row in measured.values():
            row['files'].sort()
        return measured

    @staticmethod
    def _describe_backup(root: Path, name: str, measured: Optional[Dict]) -> Dict:
        """One row: what it is, when it was taken, how big, and what it holds."""
        recognised = bool(BACKUP_STAMP_RE.match(name))
        taken = None
        if recognised:
            try:
                taken = datetime.strptime(name, BACKUP_STAMP_FORMAT)
            except ValueError:                         # pragma: no cover
                recognised = False

        files = (measured or {}).get('files', [])
        return {
            'name': name,
            'path': str(root / name),
            'recognised': recognised,
            'taken': taken.isoformat(sep=' ', timespec='seconds') if taken else '',
            'files': files,
            'file_count': len(files),
            'bytes': (measured or {}).get('bytes', 0),
            # A backup without device.conf cannot be usefully restored from, and
            # an operator choosing which to keep should be told before choosing.
            'has_device_conf': 'device.conf' in files,
        }

    def _names(self) -> Optional[list]:
        """The names in backups/, or None when the directory cannot be listed.

        The ONLY way this module asks whether a backup exists. On this host
        backups/ is `drw-rwSr--` - no execute bit for the group - and
        Path.exists(), .is_dir() and .is_file() all raise PermissionError on a
        child, measured. iterdir() on the parent works, because listing names
        needs read and traversing needs execute. So existence is a question about
        the parent's listing, never a stat on the child.
        """
        try:
            return sorted((entry.name for entry in self.backup_dir.iterdir()),
                          reverse=True)
        except (FileNotFoundError, NotADirectoryError):
            return []
        except PermissionError:
            return None

    def _delete(self, target: Path) -> Optional[str]:
        """Remove one path, directory or file. Returns an error, or None.

        Tries without privilege first and falls back to sudo, the pattern the
        rest of this layer uses. Nothing stats the target to decide which it is:
        rmtree raises NotADirectoryError for a file, which is cheaper than
        asking a question the filesystem will not answer.
        """
        try:
            shutil.rmtree(target)
            return None
        except NotADirectoryError:
            try:
                target.unlink()
                return None
            except OSError:
                pass
        except FileNotFoundError:
            return f'{target.name} is already gone'
        except OSError:
            pass

        removed = shell.sudo(['rm', '-rf', str(target)], timeout=QUICK)
        return None if removed.ok else (removed.stderr.strip() or
                                        f'could not remove {target.name}')

    def remove_backup(self, name: str) -> ServiceResult:
        """Delete one backup, by the name backups() reported.

        A name and never a path: a name with a separator in it is refused
        outright, so nothing can reach outside backups/ and there is no resolving
        or parent-checking to get wrong.
        """
        operation_id = str(uuid.uuid4())[:8]
        name = (name or '').strip()
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            return failure_result(f'{name!r} is not a backup name',
                                  ['pass the name as `config backups` lists it'],
                                  operation_id)

        names = self._names()
        if names is None:
            return failure_result(f'Cannot read {self.backup_dir}',
                                  ['the backup directory is not readable by '
                                   'this account'], operation_id)
        if name not in names:
            return failure_result(f'No backup named {name}',
                                  [f'nothing by that name in {self.backup_dir}'],
                                  operation_id)

        described = self._describe_backup(
            self.backup_dir, name, self._measure_backups(self.backup_dir).get(name))
        problem = self._delete(self.backup_dir / name)
        if problem:
            return failure_result(f'Could not remove {name}', [problem],
                                  operation_id)
        return success_result(f'Removed backup {name} '
                              f'({described["file_count"]} file(s), '
                              f'{described["bytes"] // 1024} KiB)',
                              {'name': name, **described}, operation_id)

    def prune_backups(self, *, keep: int, dry_run: bool = False) -> ServiceResult:
        """Remove all but the newest `keep` backups.

        keep must be given and must be at least one: there is no call for a
        prune that leaves nothing, and defaulting it would let a caller delete
        every backup by forgetting an argument.

        Only recognised timestamped backups are counted and removed. The legacy
        device.conf.backup.<stamp> files are left alone - they are somebody's
        deliberate copy from before this shape existed, and a prune is not the
        place to decide about them.

        dry_run reports exactly what it would remove and changes nothing, which
        is what the console shows before the operator commits.
        """
        operation_id = str(uuid.uuid4())[:8]
        if keep is None or int(keep) < 1:
            return failure_result('Keep at least one backup',
                                  ['pass keep=1 or more'], operation_id)
        keep = int(keep)

        listed = self.backups()
        rows = [b for b in (listed.data or {}).get('backups', [])
                if b['recognised']]
        doomed = rows[keep:]                      # backups() is newest first
        freed = sum(b['bytes'] for b in doomed)

        if dry_run:
            return success_result(
                f'Would remove {len(doomed)} backup(s), freeing '
                f'{freed // 1024} KiB, keeping the newest {keep}',
                {'would_remove': [b['name'] for b in doomed], 'keep': keep,
                 'bytes': freed, 'dry_run': True}, operation_id)

        removed, failed = self._delete_many([b['name'] for b in doomed])
        if failed:
            return failure_result(
                f'Removed {len(removed)} backup(s); {len(failed)} could not be '
                f'removed', failed, operation_id)
        return success_result(
            f'Removed {len(removed)} backup(s), freeing {freed // 1024} KiB, '
            f'keeping the newest {keep}',
            {'removed': removed, 'keep': keep, 'bytes': freed}, operation_id)

    def _delete_many(self, names: list) -> tuple:
        """Remove several backups, in as few calls as possible.

        ONE `rm -rf` for the whole batch rather than one per backup: a prune of
        926 - which is what this host needed - is 926 sudo invocations otherwise,
        inside a page request. The batch is tried first and the per-backup path is
        the fallback, so a single stubborn entry does not cost the others.
        """
        if not names:
            return [], []

        targets = [str(self.backup_dir / name) for name in names]
        try:
            for target in targets:
                shutil.rmtree(target)
            return list(names), []
        except OSError:
            pass                      # fall through to one privileged call

        batch = shell.sudo(['rm', '-rf'] + targets, timeout=NORMAL)
        if batch.ok:
            return list(names), []

        logger.warning('batch removal of %s backup(s) failed: %s',
                       len(targets), batch.output.strip()[:200])
        removed, failed = [], []
        for name in names:
            problem = self._delete(self.backup_dir / name)
            (failed if problem else removed).append(name)
        return removed, failed


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
