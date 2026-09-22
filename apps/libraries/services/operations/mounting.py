"""What the mount page needs: the slot map, each drive's model, and whether
each loaded cartridge can go into each drive.

Moved from the adapter's get_library_status_with_lto, check_mount_compatibility
and get_drives_for_library, which the operator AJAX endpoints called. The
compatibility answers come from tapes/compatibility.verdict, which reads
MHVTL's own drive media tables (profiles/personalities).

drive_num is the number mtx uses: the drive's `Slot:` in device.conf minus
one, not its position in the file.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import uuid
from typing import Dict, List

from ..config.service import ConfigService
from ..core import ServiceResult, failure_result, success_result
from ..profiles.personalities import density_for_barcode
from ..scsi import mapping
from ..tapes import compatibility
from . import mtx


def library_drives(library_id: int, config_dir=None) -> List[Dict]:
    """A library's drives from device.conf, ordered by drive_num."""
    conf = ConfigService(config_dir).device_conf()
    if conf is None:
        return []
    drives = []
    for drive_id, data in conf.drives_of(int(library_id)).items():
        slot = int(data.get('slot') or 0)
        model = data.get('product', '')
        drives.append({
            'drive_num': max(slot - 1, 0),
            'drive_id': drive_id,
            'vendor': data.get('vendor', ''),
            'model': model,
            'serial': data.get('serial', ''),
            'lto_generation': compatibility.lto_for_drive_model(model) or 'Unknown',
            'slot_in_library': slot,
        })
    return sorted(drives, key=lambda d: d['drive_num'])


def _tape_fields(element: Dict) -> Dict:
    barcode = element.get('barcode') if element.get('full') else None
    return {'tape_lto': compatibility.lto_for_barcode(barcode) if barcode else None,
            'tape_density': density_for_barcode(barcode) if barcode else None}


def mount_status(library_id: int, config_dir=None) -> ServiceResult:
    """The slot map with every tape's and drive's generation, and every
    loaded slot's verdict for every drive (mount_matrix).

    data carries the keys the mount page reads: drives, storage_slots,
    import_export_slots, slot_summary, compatibility_info, mount_matrix and
    drive_info.
    """
    operation_id = str(uuid.uuid4())[:8]
    device = mapping.device_for_library(library_id, config_dir=config_dir)
    if device is None:
        return failure_result(f'Could not find device for library {library_id}',
                              [f'no changer reports library {library_id}\'s SCSI '
                               f'address; is vtllibrary@{library_id} running?'],
                              operation_id)
    state = mtx.status(device)
    if not state.slots and not state.drives:
        return failure_result(f'mtx reported nothing for {device}',
                              [f'is vtllibrary@{library_id} running?'], operation_id)

    configured = {d['drive_num']: d for d in library_drives(library_id, config_dir)}

    drives = []
    for element in state.drives:
        info = configured.get(element.number, {})
        entry = {'drive_num': element.number, 'barcode': element.barcode,
                 'full': element.full, 'slot_origin': element.slot_origin,
                 'lto_generation': info.get('lto_generation', 'Unknown'),
                 'model': info.get('model', 'Unknown'),
                 'vendor': info.get('vendor', 'Unknown')}
        entry.update(_tape_fields(entry))
        drives.append(entry)

    slots = []
    for element in state.slots:
        entry = {'slot_num': element.number, 'barcode': element.barcode,
                 'full': element.full}
        entry.update(_tape_fields(entry))
        slots.append(entry)

    matrix = compatibility.mount_matrix(slots, drives)
    mountable = []
    for slot in slots:
        verdicts = matrix.get(slot['slot_num'])
        if verdicts is None:
            continue
        usable = [{'drive_num': d['drive_num'], 'lto_generation': d['lto_generation'],
                   'can_write': verdicts[d['drive_num']]['can_write']}
                  for d in drives
                  if not d['full'] and verdicts[d['drive_num']]['compatible']]
        mountable.append({'slot_num': slot['slot_num'], 'barcode': slot['barcode'],
                          'tape_lto': slot['tape_lto'],
                          'compatible_drives': usable,
                          'has_compatible_drive': bool(usable)})

    import_export = [{'slot_num': e.number, 'barcode': e.barcode, 'full': e.full}
                     for e in state.import_export]
    return success_result(
        f'Library {library_id}: {len(slots)} slots, {len(drives)} drives',
        {'library_id': library_id,
         'device_path': device,
         'drives': drives,
         'storage_slots': slots,
         'import_export_slots': import_export,
         'slot_summary': {
             'total_slots': len(slots),
             'full_slots': sum(1 for s in slots if s['full']),
             'empty_slots': sum(1 for s in slots if not s['full']),
             'total_drives': len(drives),
             'loaded_drives': sum(1 for d in drives if d['full']),
             'ie_slots': len(import_export),
         },
         'compatibility_info': mountable,
         # {slot_num: {drive_num: verdict}}; JSON turns the keys into strings
         'mount_matrix': matrix,
         'drive_info': list(configured.values())},
        operation_id)


def mount_check(library_id: int, slot: int, drive: int, config_dir=None) -> Dict:
    """Can the tape in this slot be mounted into this drive?

    Checked in this order - slot exists, slot has a tape, drive exists, drive
    is free, the drive loads the cartridge - because each answer is a
    different thing for the operator to do.
    """
    status = mount_status(library_id, config_dir)
    if not status.success:
        return {'compatible': False, 'error': status.message,
                'message': 'Could not determine compatibility'}
    data = status.data

    tape = next((s for s in data['storage_slots'] if s['slot_num'] == slot), None)
    if tape is None:
        return {'compatible': False, 'error': f'Slot {slot} not found',
                'message': f'Slot {slot} does not exist in this library'}
    if not tape['full']:
        return {'compatible': False, 'error': f'Slot {slot} is empty',
                'message': f'There is no tape in slot {slot}'}

    target = next((d for d in data['drives'] if d['drive_num'] == drive), None)
    if target is None:
        return {'compatible': False, 'error': f'Drive {drive} not found',
                'message': f'Drive {drive} does not exist in this library'}
    if target['full']:
        return {'compatible': False, 'error': f'Drive {drive} is occupied',
                'message': f'Drive {drive} already holds '
                           f'{target.get("barcode") or "a tape"}'}

    result = compatibility.verdict(tape['barcode'], target['model'])
    return {**result, 'tape_barcode': (tape['barcode'] or '').strip(),
            'drive_num': drive, 'slot': slot}
