"""End-to-end verification: does this library actually hold data?

Moved from backup_restore_test_service.py:run_full_test.

Nine steps, and the order is the whole point:

    1. generate random data            data.generate
    2. checksum it                     data.checksums
    3. mount the tape                  operations
    4. write an archive to it          tape_io.write_archive
    5. unmount it                      operations
    6. mount it again                  operations
    7. read the archive back           tape_io.read_archive
    8. checksum what came back         data.verify
    9. put the tape back in its slot   operations

Steps 5 and 6 are why this is a verification and not a file copy. Unmounting
forces the drive to flush and the robot to put the cartridge away; mounting it
again makes the read come off the medium rather than out of a buffer that still
holds what was just written. A test without them passes on a drive that is
writing to nothing.

The run stops at the first failing step. Later steps fail because that one did -
a restore cannot verify data that was never written - so continuing only buries
the cause under consequences.

Step 9 runs whatever happened before it: a verification must leave the library
as it found it. Without it the tape stayed in the drive, and the library could
not then be deleted ("1 drive(s) still have tapes loaded") until someone
unmounted it by hand.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from ..config.service import ConfigService
from ..operations import mtx
from ..operations.service import OperationsService
from ..scsi import mapping
from . import data as test_data
from . import tape_io
from .models import StepResult, VerificationReport

logger = logging.getLogger(__name__)

DEFAULT_WORK_DIR = '/tmp/mhvtl-verification'


def _timed(step: str, action: Callable[[], StepResult]) -> StepResult:
    """Run a step and record what it cost, whatever it returns or raises."""
    started = time.time()
    try:
        result = action()
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('verification step %s', step)
        result = StepResult(step=step, success=False,
                            message=f'{step} raised: {exc}', errors=[str(exc)])
    result.duration_seconds = time.time() - started
    return result


def run(library_id: int, slot: int, drive_id: int, *, size_mb: int = 10,
        file_count: int = 5, work_dir=None, cleanup_on_success: bool = True,
        config_dir=None, devices: Optional[Dict] = None) -> VerificationReport:
    """Write data to a tape, read it back and check it is the same.

    Args:
        library_id: the device.conf library id.
        slot: the slot holding the tape to test. Its contents are overwritten.
        drive_id: the device.conf drive id - 11, not 0. The mtx drive number is
            derived from it, because mtx numbers drives from 0 within a library
            and device.conf numbers them globally.
        size_mb, file_count: how much data to write.
        cleanup_on_success: remove the working directory afterwards. A failed
            run always keeps it, because the restored files are the evidence.
        devices: {'changer': node, 'drive': node} to run the whole test through
            other nodes - the library's iSCSI export, attached on this host -
            instead of the ones device.conf maps. The tape moves, the write and
            the read then all cross the iSCSI path.
    """
    run_id = str(uuid.uuid4())[:8]
    report = VerificationReport(run_id=run_id,
                                started_at=datetime.now().isoformat())
    root = Path(work_dir or DEFAULT_WORK_DIR) / run_id
    devices = devices or {}
    operations = OperationsService(config_dir, changer_device=devices.get('changer'))

    drive_number = _mtx_drive_number(library_id, drive_id, config_dir)
    if drive_number is None:
        report.add(StepResult(
            step='resolve drive', success=False,
            message=f'Drive {drive_id} is not a drive of library {library_id}',
            errors=['check device.conf']))
        report.finished_at = datetime.now().isoformat()
        return report

    source = root / 'source'
    restored = root / 'restored'
    checksums: Dict[str, Dict] = {}

    def generate():
        files = test_data.generate(source, size_mb=size_mb, file_count=file_count)
        return StepResult(step='generate data', success=True,
                          message=f'{len(files)} files, about {size_mb}MB',
                          data={'directory': str(source), 'files': files})

    def checksum():
        nonlocal checksums
        checksums = test_data.checksums(source)
        return StepResult(step='checksum data', success=bool(checksums),
                          message=f'{len(checksums)} files checksummed',
                          data={'files': len(checksums)})

    def mount():
        result = operations.mount(library_id, slot, drive_number)
        return StepResult(step='mount tape', success=result.success,
                          message=result.message, errors=list(result.errors))

    def write():
        device = devices.get('drive') or _device_for(drive_id, config_dir)
        if device is None:
            return StepResult(step='write archive', success=False,
                              message=f'No tape device for drive {drive_id}',
                              errors=['no SCSI device reports its address'])
        result = tape_io.write_archive(tape_io.writable_device(device), source)
        return StepResult(step='write archive', success=result.ok,
                          message=('archive written' if result.ok
                                   else 'tar failed writing to the tape'),
                          data={'device': device},
                          errors=[] if result.ok else [result.output.strip()[:300]])

    def unmount():
        result = operations.unmount(library_id, drive_number, slot=slot)
        return StepResult(step='unmount tape', success=result.success,
                          message=result.message, errors=list(result.errors))

    def remount():
        result = operations.mount(library_id, slot, drive_number)
        return StepResult(step='mount tape again', success=result.success,
                          message=result.message, errors=list(result.errors))

    def read():
        device = devices.get('drive') or _device_for(drive_id, config_dir)
        if device is None:
            return StepResult(step='read archive', success=False,
                              message=f'No tape device for drive {drive_id}',
                              errors=['no SCSI device reports its address'])
        result = tape_io.read_archive(tape_io.writable_device(device), restored)
        return StepResult(step='read archive', success=result.ok,
                          message=('archive read back' if result.ok
                                   else 'tar failed reading from the tape'),
                          data={'device': device},
                          errors=[] if result.ok else [result.output.strip()[:300]])

    def verify():
        found = tape_io.extracted_root(restored)
        verified, corrupted, missing = test_data.verify(found, checksums)
        ok = not corrupted and not missing
        detail = (f'{len(verified)} of {len(checksums)} files verified')
        if not ok:
            detail += f', {len(corrupted)} corrupted, {len(missing)} missing'
        return StepResult(step='verify checksums', success=ok, message=detail,
                          data={'verified': len(verified),
                                'corrupted': corrupted, 'missing': missing,
                                'directory': str(found)})

    def put_back():
        """Return the tape to its slot, so the library is as it was."""
        device = devices.get('changer') or mapping.device_for_library(
            library_id, config_dir=config_dir)
        state = mtx.status(device) if device else None
        loaded = state.drive(drive_number) if state else None
        if loaded is None or not loaded.full:
            return StepResult(step='return the tape', success=True,
                              message='the drive is already empty')
        result = operations.unmount(library_id, drive_number, slot=slot)
        return StepResult(step='return the tape', success=result.success,
                          message=result.message, errors=list(result.errors or []))

    for step, action in (('generate data', generate), ('checksum data', checksum),
                         ('mount tape', mount), ('write archive', write),
                         ('unmount tape', unmount), ('mount tape again', remount),
                         ('read archive', read), ('verify checksums', verify)):
        if not report.add(_timed(step, action)).success:
            break

    # Whatever happened above, the cartridge goes back where it came from.
    report.add(_timed('return the tape', put_back))

    report.finished_at = datetime.now().isoformat()
    report.summary = {'library_id': library_id, 'slot': slot,
                      'drive_id': drive_id, 'drive_number': drive_number,
                      'size_mb': size_mb, 'file_count': file_count,
                      'work_dir': str(root),
                      'changer_device': devices.get('changer'),
                      'drive_device': devices.get('drive')}

    if report.success and cleanup_on_success:
        cleanup(root)
    elif not report.success:
        logger.warning('verification %s failed at %s; keeping %s', run_id,
                       report.failed_step.step if report.failed_step else '?', root)

    return report


def cleanup(root: Path) -> bool:
    """Remove a run's working directory. Kept after a failure, as evidence."""
    try:
        shutil.rmtree(root)
        return True
    except OSError as exc:
        logger.warning('could not remove %s: %s', root, exc)
        return False


def _mtx_drive_number(library_id: int, drive_id: int,
                      config_dir=None) -> Optional[int]:
    """The number mtx uses for a drive, from its device.conf id.

    mtx numbers a library's drives from 0; device.conf numbers them globally, so
    library 20's first drive is 21 to device.conf and 0 to mtx. The mapping is
    the drive's position among its library's drives, in id order - not
    drive_id - library_id, which is only the same while no drive has been
    removed.
    """
    conf = ConfigService(config_dir).device_conf()
    if conf is None:
        return None
    drives = sorted(conf.drives_of(int(library_id)))
    try:
        return drives.index(int(drive_id))
    except ValueError:
        return None


def _device_for(drive_id: int, config_dir=None) -> Optional[str]:
    """The /dev node for a drive, matched on its SCSI address."""
    return mapping.device_for_drive(int(drive_id), config_dir=config_dir)
