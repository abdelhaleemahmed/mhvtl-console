"""Mounting an LTFS cartridge, for the web console and the command line alike.

Everything here is one call in and a ServiceResult out, so the page and the CLI
are both callers and neither owns anything. That is the reason this is not a
shell script: a feature only a terminal can reach would be the one thing this
console cannot offer.

WHAT THIS DOES NOT KEEP
-----------------------
No state. A mount's existence is read from the kernel (ltfs/mounts.py), which
drive a mount belongs to is read from its own mount source, which cartridge is
in that drive comes from the robot, and whether LTFS can use the drive at all
comes from profiles/ltfs_support.py. Four authoritative sources composed on
demand beat
one registry that can disagree with all of them - see the note in mounts.py.

THE DESTRUCTIVE ONE
-------------------
mkltfs partitions and formats a cartridge and there is no undo. It is off by
default for the console (MHVTL_GUI_ALLOW_LTFS_FORMAT) and refused here rather
than left to fail as a sudo error, so the refusal says what it is. The packaged
sudoers deliberately does not grant mkltfs to the service account either; a
human with root can always run it.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from . import mounts
from ..core import QUICK, SLOW, failure_result, shell, success_result
from ..core.results import ServiceResult
from ..operations import mounting, mtx
from ..profiles import ltfs_support
from ..scsi import lsscsi, mapping
from ..tapes import ltfs_state as cartridge, media

logger = logging.getLogger(__name__)

#: Where volumes are mounted. One directory per drive underneath, so two drives
#: can hold volumes at once - and because the packaged sudoers grants
#: `fusermount -u /var/lib/ltfs/mnt/*`, which is children only, never the root.
MOUNT_ROOT = Path('/var/lib/ltfs/mnt')

#: The binaries, in the order the RPM and a source install put them.
SEARCH_PATH = ('/usr/bin', '/usr/local/bin')

#: ltfsck's exit status is a BIT FIELD, not a sequence (ltfs_error.h:511-530):
#:
#:     0x00  NO_ERRORS          consistent, cartridge untouched
#:     0x01  CORRECTED          consistent, cartridge was modified
#:     0x02  REBOOT_REQUIRED
#:     0x04  UNCORRECTED        cannot recover
#:     0x08  OPERATIONAL_ERROR  device error while checking
#:     0x10  USAGE_SYNTAX_ERROR
#:
#: So the volume is sound when nothing outside these two bits is set. Testing
#: `returncode in (0, 1)` - which an earlier version of this did - calls exit 3,
#: a corrected volume that also wants a reboot, a failure.
LTFSCK_CORRECTED = 0x01
LTFSCK_REBOOT_REQUIRED = 0x02
LTFSCK_BENIGN = LTFSCK_CORRECTED | LTFSCK_REBOOT_REQUIRED


def _binary(name: str) -> Optional[str]:
    """The absolute path to an LTFS binary, or None when it is not installed.

    Absolute because sudo matches its rules on the resolved path, and because
    /usr/local/bin is not on the default secure_path - a source install would
    otherwise fail with "command not found" while the binary sits right there.
    """
    for folder in SEARCH_PATH:
        candidate = Path(folder) / name
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def installed() -> Dict[str, Optional[str]]:
    """Which of the four LTFS commands this host has."""
    return {name: _binary(name)
            for name in ('ltfs', 'mkltfs', 'ltfsck', 'ltfs_ordered_copy')}


def _allow_format() -> bool:
    try:
        from django.conf import settings
        return bool(getattr(settings, 'MHVTL_GUI_ALLOW_LTFS_FORMAT', False))
    except Exception:            # noqa: BLE001 - no Django, or settings not ready
        return False


def mount_point_for(library_id: int, drive_num: int) -> Path:
    """One directory per drive, named so an operator can tell whose it is."""
    return MOUNT_ROOT / f'library{int(library_id)}-drive{int(drive_num)}'


class LtfsService:
    """Read LTFS state for a library's drives, and mount or unmount a volume."""

    def __init__(self, config_directory=None, media_directory=None):
        # Named as every other service in this tree names them, so a caller
        # holding several does not have to remember which one is different.
        self.config_dir = config_directory
        self.media_dir = media_directory

    # -- reading ------------------------------------------------------------

    def status(self, library_id: int) -> ServiceResult:
        """Every drive in the library: what it is, whether LTFS can use it,
        what is loaded, and whether that is mounted.

        Costs one mtx call and one lsscsi however many drives the library has,
        because the device list is discovered once here and handed to every
        mapping lookup. Without that each lookup runs its own `sudo lsscsi` -
        which is the shape media.usage_for_all() was written to avoid, where
        per-item reads at about 69ms each cost a 32-cartridge library over four
        seconds on every page load. This endpoint is meant to be polled.

        The mount state is a file read and needs no privilege at all.
        """
        drives = mounting.library_drives(library_id, self.config_dir)
        if not drives:
            return failure_result(
                f'Library {library_id} has no drives in device.conf',
                [f'No drive entries for library {library_id}'])

        devices = lsscsi.local()
        changer = mapping.device_for_library(library_id, devices=devices,
                                             config_dir=self.config_dir)
        loaded: Dict[int, Optional[str]] = {}
        if changer:
            for element in mtx.status(changer).drives:
                loaded[element.number] = element.barcode if element.full else None

        mounted = mounts.by_device()

        barcodes = [b for b in loaded.values() if b]
        usage = media.usage_for_all(barcodes, self.media_dir) if barcodes else {}
        states = cartridge.state_for_all(
            barcodes, {b: u.partitions for b, u in usage.items()},
            self.media_dir) if barcodes else {}

        rows: List[Dict] = []
        for drive in drives:
            node = mapping.device_for_drive(drive['drive_id'], devices=devices,
                                            generic=True,
                                            config_dir=self.config_dir)
            support = ltfs_support.supports(drive['vendor'], drive['model'],
                                            drive.get('revision') or None)
            barcode = loaded.get(drive['drive_num'])
            state = states.get(barcode) if barcode else None
            rows.append({
                'drive_id': drive['drive_id'],
                'drive_num': drive['drive_num'],
                'vendor': drive['vendor'],
                'model': drive['model'],
                'revision': drive.get('revision') or '',
                'device': node,
                'ltfs_capable': support.supported,
                'ltfs_reason': support.reason,
                'barcode': barcode,
                'cartridge_state': state.state if state else '',
                'cartridge_summary': state.summary if state else '',
                # A wiped cartridge reports `plain` with this set. Without it a
                # JSON caller on this endpoint cannot tell one from a cartridge
                # that was never formatted, which the tape rows can.
                'cartridge_was_ltfs': state.was_ltfs if state else False,
                'mounted_at': mounted.get(node) if node else None,
            })

        tools = installed()
        return success_result(
            f'Library {library_id}: {sum(1 for r in rows if r["mounted_at"])} '
            f'of {len(rows)} drive(s) hold a mounted volume',
            {'library_id': library_id, 'drives': rows, 'tools': tools,
             'format_allowed': _allow_format()})

    # -- mounting -----------------------------------------------------------

    def mount(self, library_id: int, drive_num: int) -> ServiceResult:
        """Mount the cartridge in this drive as a filesystem.

        The mount outlives this call: ltfs daemonises once the volume is open,
        which is why nothing here has to hold a handle. Refusals are checked in
        the order an operator would hit them, so the message names the first
        real problem rather than the last symptom.
        """
        checked = self._resolve(library_id, drive_num, need_cartridge=True)
        if isinstance(checked, ServiceResult):
            return checked
        drive, node, barcode, state = checked

        binary = _binary('ltfs')
        if not binary:
            return failure_result('LTFS is not installed on this host',
                                  ['No ltfs binary in /usr/bin or /usr/local/bin'])
        if state and state.state != cartridge.LTFS:
            hint = ('It has LTFS attributes but only one partition - it was '
                    'formatted once and has since been unpartitioned, so it '
                    'cannot be mounted.' if state.was_ltfs else
                    'Format it with mkltfs first, which erases it.')
            return failure_result(f'{barcode} is not an LTFS volume', [hint])

        point = mount_point_for(library_id, drive_num)
        already = mounts.point_for(node)
        if already:
            return success_result(f'{barcode} is already mounted at {already}',
                                 {'barcode': barcode, 'mount_point': already,
                                  'device': node})

        created = shell.sudo(['mkdir', '-p', str(point)])
        if not created.ok:
            return failure_result(f'Could not create {point}',
                                  [created.output.strip()[:300]])

        # No work_directory or sync_type here: /etc/ltfs.conf.local carries them
        # for every run, so the console cannot drift from the command line.
        result = shell.sudo([binary, '-o', f'devname={node}', str(point)],
                            timeout=SLOW)
        mounted_at = mounts.point_for(node)
        if not mounted_at:
            return failure_result(
                f'{barcode} did not mount',
                [result.output.strip()[:500] or 'ltfs said nothing'])
        return success_result(f'{barcode} mounted at {mounted_at}',
                              {'barcode': barcode, 'mount_point': mounted_at,
                               'device': node, 'drive_num': drive_num})

    def unmount(self, library_id: int, drive_num: int) -> ServiceResult:
        """Release the volume. This is when LTFS writes its index to the tape.

        Killing the process instead leaves the data on the cartridge and the
        index behind it, which is what ltfsck exists to repair - so the unmount
        is a first-class operation and not an afterthought.
        """
        checked = self._resolve(library_id, drive_num, need_cartridge=False)
        if isinstance(checked, ServiceResult):
            return checked
        _, node, barcode, _ = checked

        point = mounts.point_for(node)
        if not point:
            return success_result(f'Drive {drive_num} holds no mounted volume',
                                  {'drive_num': drive_num, 'device': node})

        # Refuse a path outside our own tree: the sudoers rule allows only
        # children of MOUNT_ROOT, and this says so before sudo does.
        if MOUNT_ROOT not in Path(point).parents:
            return failure_result(
                f'{point} is not inside {MOUNT_ROOT}',
                ['Refusing to unmount a path this service did not create'])

        result = shell.sudo(['fusermount', '-u', point], timeout=SLOW)
        if mounts.point_for(node):
            return failure_result(
                f'{point} is still mounted',
                [result.output.strip()[:300] or 'fusermount said nothing',
                 'A process with the mount point open will hold it'])
        # Take the directory away again, so a host does not collect one per
        # drive per library for ever. rmdir and not rm -rf: if anything is left
        # in there it is not ours to delete, and a failure is not worth
        # reporting - the unmount is what the caller asked for and it worked.
        shell.sudo(['rmdir', point], timeout=QUICK)
        return success_result(f'{point} unmounted; the index is on the cartridge',
                              {'drive_num': drive_num, 'device': node,
                               'barcode': barcode, 'was_mounted_at': point})

    # -- checking and formatting -------------------------------------------

    def check(self, library_id: int, drive_num: int) -> ServiceResult:
        """ltfsck the cartridge in this drive.

        Exit 1 is LTFSCK_CORRECTED and means success with the cartridge
        modified - fsck's convention. Treating non-zero as failure here would
        report every healthy volume as broken, and treating the status as a
        sequence rather than a bit field would fail exit 3. See LTFSCK_BENIGN.
        """
        checked = self._resolve(library_id, drive_num, need_cartridge=True)
        if isinstance(checked, ServiceResult):
            return checked
        _, node, barcode, _ = checked

        binary = _binary('ltfsck')
        if not binary:
            return failure_result('ltfsck is not installed on this host', [])
        if mounts.point_for(node):
            return failure_result(
                f'{barcode} is mounted', ['Unmount it before checking it'])

        result = shell.sudo([binary, node], timeout=SLOW)
        # A timeout reports -1, which must not be read as a bit field.
        benign = (not result.timed_out
                  and result.returncode >= 0
                  and not result.returncode & ~LTFSCK_BENIGN)
        consistent = benign or 'LTFS16022I' in result.output
        message = (f'{barcode} is consistent' if consistent
                   else f'{barcode} did not pass')
        data = {'barcode': barcode, 'device': node,
                'exit_code': result.returncode,
                'corrected': bool(result.returncode & LTFSCK_CORRECTED)
                             if result.returncode > 0 else False,
                'reboot_required': bool(result.returncode & LTFSCK_REBOOT_REQUIRED)
                                   if result.returncode > 0 else False,
                'output': result.output.strip()[-2000:]}
        return success_result(message, data) if consistent else \
            failure_result(message, [result.output.strip()[-500:]])

    def format_cartridge(self, library_id: int, drive_num: int) -> ServiceResult:
        """mkltfs the cartridge in this drive. THIS ERASES IT.

        Refused unless MHVTL_GUI_ALLOW_LTFS_FORMAT is set, so the console says
        no in its own words instead of surfacing a sudo error - and so that
        turning it on is a deliberate act recorded in a settings file.
        """
        if not _allow_format():
            return failure_result(
                'Formatting a cartridge is disabled on this host',
                ['mkltfs partitions and erases the cartridge; there is no undo.',
                 'Set MHVTL_GUI_ALLOW_LTFS_FORMAT to enable it, and note the '
                 'packaged sudoers does not grant mkltfs to the service account.'])

        checked = self._resolve(library_id, drive_num, need_cartridge=True)
        if isinstance(checked, ServiceResult):
            return checked
        drive, node, barcode, _ = checked

        binary = _binary('mkltfs')
        if not binary:
            return failure_result('mkltfs is not installed on this host', [])
        if mounts.point_for(node):
            return failure_result(
                f'{barcode} is mounted', ['Unmount it before formatting it'])

        support = ltfs_support.supports(drive['vendor'], drive['model'],
                                        drive.get('revision') or None)
        if not support.supported:
            return failure_result(
                f'LTFS will not open drive {drive_num}', [support.reason])

        result = shell.sudo([binary, '-d', node], timeout=SLOW)
        if 'LTFS15024I' not in result.output:
            return failure_result(
                f'{barcode} was not formatted',
                [result.output.strip()[-500:] or 'mkltfs said nothing'])
        return success_result(f'{barcode} formatted as an LTFS volume',
                              {'barcode': barcode, 'device': node,
                               'output': result.output.strip()[-2000:]})

    # -- shared resolution --------------------------------------------------

    def _resolve(self, library_id: int, drive_num: int, *, need_cartridge: bool):
        """(drive, node, barcode, cartridge state) or the ServiceResult to return.

        One place for the four things every verb needs, so they refuse in the
        same order with the same words.
        """
        drives = mounting.library_drives(library_id, self.config_dir)
        drive = next((d for d in drives if d['drive_num'] == int(drive_num)), None)
        if drive is None:
            return failure_result(
                f'Library {library_id} has no drive {drive_num}',
                [f'Drives are numbered 0 to {len(drives) - 1}' if drives
                 else f'Library {library_id} has no drives in device.conf'])

        # Discovery happens here and not above, so an unknown drive number - a
        # pure device.conf question - is refused without any subprocess at all.
        # Past this point one is unavoidable: the node comes from lsscsi and
        # whether a cartridge is loaded comes from mtx, and neither can be known
        # without asking. One discovery serves both lookups, for the reason
        # status() gives.
        devices = lsscsi.local()
        node = mapping.device_for_drive(drive['drive_id'], devices=devices,
                                        generic=True,
                                        config_dir=self.config_dir)
        if not node:
            return failure_result(
                f'Drive {drive_num} has no device node',
                ['Its address is in device.conf but no tape device reports it; '
                 'the drive daemon may not be running'])

        barcode = None
        changer = mapping.device_for_library(library_id, devices=devices,
                                             config_dir=self.config_dir)
        if changer:
            element = mtx.status(changer).drive(int(drive_num))
            barcode = element.barcode if element and element.full else None
        if need_cartridge and not barcode:
            return failure_result(f'Drive {drive_num} holds no cartridge',
                                 ['Load one with `mhvtl op mount`'])

        state = None
        if barcode:
            usage = media.usage_for_all([barcode], self.media_dir)
            state = cartridge.state_for_all(
                [barcode], {barcode: usage[barcode].partitions if barcode in usage
                            else 0}, self.media_dir).get(barcode)
        return drive, node, barcode, state
