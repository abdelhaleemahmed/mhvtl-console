"""Mount, unmount, move, online, offline.

Moved from tape_operations_service.py:1216 (mount), :1282 (unmount), :1348
(move), :1418 (online) and :1430 (offline).

Two things this module insists on, both learned from bugs:

    - the device comes from scsi.mapping, which matches the SCSI address
      device.conf assigns. The original picked the Nth changer lsscsi reported,
      which sends an operation to a different library whenever the two orders
      disagree;
    - vtlcmd is addressed by the device.conf id, not library_id // 10;
    - a mount is checked against the drive's media list before the robot
      moves anything (tapes/compatibility.verdict).

Every operation checks the library's current state before acting, so "slot 5 is
empty" is reported as that rather than as an mtx exit code.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from typing import Optional

from ..core import ServiceResult, failure_result, success_result
from ..scsi import mapping
from ..tapes import compatibility
from . import mtx, vtlcmd

logger = logging.getLogger(__name__)


class OperationsService:
    """Moving tapes, and taking libraries in and out of service.

        ops = OperationsService()
        ops.mount(library_id=10, slot=1, drive=0)
        ops.status(10)
    """

    def __init__(self, config_directory=None, *, changer_device: str = None):
        """changer_device drives the library through another node - its iSCSI
        export attached on this host - instead of the one device.conf maps."""
        self.config_dir = config_directory
        self.changer_device = changer_device

    # -- reading ----------------------------------------------------------

    def _device(self, library_id: int) -> Optional[str]:
        if self.changer_device:
            return self.changer_device
        return mapping.device_for_library(library_id, config_dir=self.config_dir)

    def status(self, library_id: int) -> ServiceResult:
        """The library's slot map, read with mtx."""
        operation_id = str(uuid.uuid4())[:8]
        device = self._device(library_id)
        if device is None:
            return self._no_device(library_id, operation_id)

        state = mtx.status(device)
        if not state.slots and not state.drives:
            return failure_result(
                f'Library {library_id} did not answer',
                [f'mtx reported nothing for {device}; is vtllibrary@{library_id} '
                 f'running?'], operation_id)

        return success_result(
            f'Library {library_id}: {state.summary["full_slots"]} of '
            f'{state.summary["total_slots"]} slots full, '
            f'{state.summary["loaded_drives"]} of {state.summary["total_drives"]} '
            f'drives loaded',
            {'library_id': library_id, **state.to_dict()}, operation_id)

    # -- moving tapes -----------------------------------------------------

    def _drive_model(self, library_id: int, drive: int) -> Optional[str]:
        """The product string device.conf gives the library's `drive` (the
        mtx number, from 0)."""
        from .mounting import library_drives
        found = next((d for d in library_drives(library_id, self.config_dir)
                      if d['drive_num'] == drive), None)
        return found['model'] if found else None

    def mount(self, library_id: int, slot: int, drive: int,
              force: bool = False) -> ServiceResult:
        """Load the tape in `slot` into `drive`.

        Refuses a cartridge the drive's MHVTL personality does not load, unless
        `force`: the library would move it and the drive would then unload it
        and keep it (usr/cmd/vtltape.c:1512), so the operator gets a drive with
        a tape it will not read instead of an error. A tape or drive this
        cannot recognise is mounted, and MHVTL decides.
        """
        operation_id = str(uuid.uuid4())[:8]
        device = self._device(library_id)
        if device is None:
            return self._no_device(library_id, operation_id)

        state = mtx.status(device)

        source = state.slot(slot)
        if source is None:
            return failure_result(f'Slot {slot} does not exist in library {library_id}',
                                  [f'the library has {len(state.slots)} slots'],
                                  operation_id)
        if not source.full:
            return failure_result(f'Slot {slot} is empty', ['nothing to mount'],
                                  operation_id)

        target = state.drive(drive)
        if target is None:
            return failure_result(f'Drive {drive} does not exist in library {library_id}',
                                  [f'the library has {len(state.drives)} drives'],
                                  operation_id)
        if target.full:
            return failure_result(
                f'Drive {drive} already holds {target.barcode or "a tape"}',
                ['unmount it first'], operation_id)

        fit = compatibility.verdict(source.barcode, self._drive_model(library_id, drive))
        if not fit['compatible'] and not force:
            return failure_result(
                f'Not mounting {source.barcode}: {fit["message"]}',
                ['MHVTL would move the cartridge and then refuse to load it, '
                 'leaving it in the drive',
                 'choose a drive that takes it, or mount with force'],
                operation_id)

        result = mtx.load(device, slot, drive)
        if not result.ok:
            return failure_result(f'Could not mount slot {slot} into drive {drive}',
                                  [result.output.strip()[:300]], operation_id)

        note = '' if fit['can_write'] else ' (read-only in this drive)'
        return success_result(
            f'Mounted {source.barcode} from slot {slot} into drive {drive}{note}',
            {'library_id': library_id, 'slot': slot, 'drive': drive,
             'barcode': source.barcode, 'compatibility': fit}, operation_id)

    def unmount(self, library_id: int, drive: int,
                slot: Optional[int] = None) -> ServiceResult:
        """Return the tape in `drive` to a slot - its own by default."""
        operation_id = str(uuid.uuid4())[:8]
        device = self._device(library_id)
        if device is None:
            return self._no_device(library_id, operation_id)

        state = mtx.status(device)
        source = state.drive(drive)
        if source is None:
            return failure_result(f'Drive {drive} does not exist in library {library_id}',
                                  [f'the library has {len(state.drives)} drives'],
                                  operation_id)
        if not source.full:
            return failure_result(f'Drive {drive} is empty', ['nothing to unmount'],
                                  operation_id)

        # mtx needs a destination. The drive line records where the tape came
        # from, which is where it belongs.
        destination = slot if slot is not None else source.slot_origin
        if destination is None:
            empty = next((s for s in state.slots if not s.full), None)
            if empty is None:
                return failure_result(
                    f'Nowhere to put the tape from drive {drive}',
                    ['every storage slot is full'], operation_id)
            destination = empty.number

        result = mtx.unload(device, destination, drive)
        if not result.ok:
            return failure_result(
                f'Could not unmount drive {drive} to slot {destination}',
                [result.output.strip()[:300]], operation_id)

        return success_result(
            f'Unmounted {source.barcode} from drive {drive} to slot {destination}',
            {'library_id': library_id, 'drive': drive, 'slot': destination,
             'barcode': source.barcode}, operation_id)

    def move(self, library_id: int, from_slot: int, to_slot: int) -> ServiceResult:
        """Move a tape between two storage slots."""
        operation_id = str(uuid.uuid4())[:8]
        device = self._device(library_id)
        if device is None:
            return self._no_device(library_id, operation_id)

        state = mtx.status(device)

        source = state.slot(from_slot)
        target = state.slot(to_slot)
        if source is None or target is None:
            missing = from_slot if source is None else to_slot
            return failure_result(f'Slot {missing} does not exist in library {library_id}',
                                  [f'the library has {len(state.slots)} slots'],
                                  operation_id)
        if not source.full:
            return failure_result(f'Slot {from_slot} is empty', ['nothing to move'],
                                  operation_id)
        if target.full:
            return failure_result(
                f'Slot {to_slot} already holds {target.barcode or "a tape"}',
                ['choose an empty slot'], operation_id)

        result = mtx.transfer(device, from_slot, to_slot)
        if not result.ok:
            return failure_result(f'Could not move slot {from_slot} to {to_slot}',
                                  [result.output.strip()[:300]], operation_id)

        return success_result(
            f'Moved {source.barcode} from slot {from_slot} to slot {to_slot}',
            {'library_id': library_id, 'from_slot': from_slot, 'to_slot': to_slot,
             'barcode': source.barcode}, operation_id)

    # -- library state ----------------------------------------------------

    def online(self, library_id: int) -> ServiceResult:
        """Bring a library online through its message queue."""
        return self._vtlcmd(library_id, vtlcmd.online, 'online')

    def offline(self, library_id: int) -> ServiceResult:
        """Take a library offline. The daemon refuses while a drive holds a tape."""
        return self._vtlcmd(library_id, vtlcmd.offline, 'offline')

    # -- the MAP (import/export port) --------------------------------------

    #: What each MAP verb does, and what to say when it worked. The MAP is the
    #: mail slot: an operator opens it, puts a cartridge in, closes it, and the
    #: robot loads it only once told to.
    MAP_ACTIONS = {
        'open': (vtlcmd.open_map, 'MAP door opened'),
        'close': (vtlcmd.close_map, 'MAP door closed'),
        'load': (vtlcmd.load_map, 'MAP inventoried'),
        'list': (vtlcmd.list_map, 'MAP contents listed'),
        'empty': (vtlcmd.empty_map, 'MAP emptied'),
    }

    def map_command(self, library_id: int, action: str) -> ServiceResult:
        """Run one MAP verb against a library's robot.

        Matched against a fixed set rather than interpolated into the command:
        this is reachable from a web form, and vtlcmd takes plenty of other
        verbs an operator should not be able to reach that way.
        """
        operation_id = str(uuid.uuid4())[:8]
        entry = self.MAP_ACTIONS.get((action or '').lower())
        if entry is None:
            return failure_result(
                f'Unknown MAP action {action!r}',
                ['expected ' + ', '.join(sorted(self.MAP_ACTIONS))], operation_id)

        command, message = entry
        result = command(int(library_id))
        if not result.ok:
            return failure_result(
                f'MAP {action} failed on library {library_id}',
                [result.output.strip()[:300] or
                 f'is vtllibrary@{library_id} running?'], operation_id)

        return success_result(f'Library {library_id}: {message}',
                              {'library_id': int(library_id), 'action': action,
                               'output': result.stdout.strip()}, operation_id)

    def _vtlcmd(self, library_id: int, action, name: str) -> ServiceResult:
        operation_id = str(uuid.uuid4())[:8]
        result = action(library_id)
        if not result.ok:
            return failure_result(
                f'Could not take library {library_id} {name}',
                [result.output.strip()[:300] or
                 f'vtlcmd {library_id} {name} failed; is vtllibrary@{library_id} '
                 f'running?'], operation_id)
        return success_result(f'Library {library_id} is {name}',
                              {'library_id': library_id, 'state': name,
                               'output': result.output.strip()}, operation_id)

    def drive_stats(self, drive_id: int) -> ServiceResult:
        """Live byte counters for a drive, readable while a backup is running."""
        operation_id = str(uuid.uuid4())[:8]
        stats = vtlcmd.stats(drive_id)
        if stats is None:
            return failure_result(
                f'Drive {drive_id} did not report statistics',
                ['the daemon may predate the vtlcmd stats patch, or not be running'],
                operation_id)

        loaded = f'{stats.barcode} loaded' if stats.loaded else 'no tape loaded'
        return success_result(f'Drive {drive_id}: {loaded}', stats.to_dict(),
                              operation_id)

    @staticmethod
    def _no_device(library_id: int, operation_id: str) -> ServiceResult:
        return failure_result(
            f'No device found for library {library_id}',
            [f'device.conf gives library {library_id} a SCSI address that no '
             f'changer reports. Check that vtllibrary@{library_id} is running '
             f'and that the mhvtl module is loaded.'], operation_id)
