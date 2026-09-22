"""
Tape Operations Views
Django views for tape library operations (mount, unmount, move, status),
tape and drive management, and the barcode helpers the forms call.

Every view calls apps/libraries/services directly: services/operations for
moving tapes and reading mtx/mt, services/tapes for tapes and barcodes,
services/drives and services/libraries for the configuration, services/scsi
for discovery. They used to go through adapters/tape_operations_service.py and
adapters/mhvtl_library_service.py; the few helpers below that shape a service
answer into what a template or its JavaScript reads are what is left of that.

Location: apps/libraries/tape_operations_views.py
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views import View
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
import json
import logging

from .models import Library, Drive
from .services.config.service import ConfigService
from .services.console import units
from .services.core import failure_result
from .post_only import RedirectOnGet
from .services.drives import DriveService
from .services.libraries import LibraryService
from .services.operations import OperationsService, mt, mtx
from .services.profiles import personalities
from .services.profiles.data import get_profile
from .services.scsi import lsscsi, mapping
from .services.tapes import TapeService, barcodes

#: The operator dashboard template still reads this flag; the services are
#: part of this app, so they are always there.
TAPE_SERVICE_AVAILABLE = True

logger = logging.getLogger(__name__)


# ============================================================================
# Helper Functions
# ============================================================================

def check_login(request):
    """Check if user is logged in"""
    return request.session.get('mhvtl_logged_in', False)


def _db_libraries(db_libraries):
    return [
        {
            'library_id': lib.library_id,
            'vendor': lib.vendor_identification,
            'product': lib.product_identification,
            'serial': lib.unit_serial_number,
            'brand': lib.brand,
            'model': lib.model,
        }
        for lib in db_libraries
    ]


def get_live_libraries():
    """
    Return library list using live MHVTL config as authoritative source,
    joined with DB records — same pattern as LibraryListView.
    Falls back to DB-only if device.conf cannot be read.
    """
    db_libraries = Library.objects.filter(is_active=True).select_related('brand', 'model').order_by('library_id')

    try:
        listed = LibraryService().list(with_contents=False)
        configured = (listed.data or {}).get('libraries', []) if listed.success else []
    except Exception:                                  # noqa: BLE001 - fall back
        logger.exception('listing configured libraries')
        configured = []

    if not configured:
        return _db_libraries(db_libraries)

    db_map = {lib.library_id: lib for lib in db_libraries}
    libraries = []
    for lib in configured:
        db_lib = db_map.get(lib['library_id'])
        if db_lib is None:
            continue
        libraries.append({
            'library_id': lib['library_id'],
            'vendor': lib['vendor'],
            'product': lib['product'],
            'serial': lib.get('serial') or db_lib.unit_serial_number,
            'brand': db_lib.brand,
            'model': db_lib.model,
        })
    return libraries


def _operations():
    return OperationsService()


def _tapes():
    return TapeService()


def _as_json(result) -> dict:
    """A ServiceResult as these endpoints have always returned it."""
    body = result.to_dict()
    body['command_output'] = (result.data or {}).get('output')
    return body


def _inventory_drift(library_id: int, state) -> dict:
    """What the robot reports against what library_contents says.

    The daemon reads library_contents once, when it starts, so a tape created
    or removed since then is in the file and not in the robot - or the other
    way round. Nothing said so: the status page simply showed the old
    inventory, which is how a tape created minutes earlier appeared to be
    missing.
    """
    contents = ConfigService().library_contents(library_id)
    if contents is None:
        return {}

    configured = set(contents.barcodes)
    reported = {slot.barcode for slot in
                list(state.slots) + list(state.drives) + list(state.import_export)
                if slot.barcode}
    added, removed = sorted(configured - reported), sorted(reported - configured)
    if not added and not removed:
        return {}
    return {'added': added, 'removed': removed,
            'configured': len(configured), 'reported': len(reported)}


def _library_status(library_id: int) -> dict:
    """mtx status for one library, in the shape library_status.html and the
    status endpoint read.

    The adapter this replaces read a picker_count mtx.LibraryStatus does not
    have, so the status page and /libraries/ajax/library-status/ failed with
    AttributeError; nothing read the count.
    """
    base = {'library_id': library_id, 'storage_slots': [], 'drives': [],
            'import_export_slots': [], 'raw_output': ''}
    device = mapping.device_for_library(int(library_id))
    if not device:
        return {**base, 'success': False, 'device_path': '',
                'slot_summary': mtx.LibraryStatus().summary,
                'error': f'Could not find device for library {library_id}'}

    state = mtx.status(device)
    if not state.slots and not state.drives:
        return {**base, 'success': False, 'device_path': device,
                'slot_summary': state.summary, 'raw_output': state.raw,
                'error': f'mtx reported nothing for {device}; is '
                         f'vtllibrary@{library_id} running?'}

    return {
        'success': True,
        'library_id': library_id,
        'device_path': device,
        'drift': _inventory_drift(library_id, state),
        'storage_slots': [{'slot_num': s.number, 'barcode': s.barcode, 'full': s.full}
                          for s in state.slots],
        'drives': [{'drive_num': d.number, 'barcode': d.barcode, 'full': d.full,
                    'slot_origin': d.slot_origin} for d in state.drives],
        'import_export_slots': [{'slot_num': s.number, 'barcode': s.barcode,
                                 'full': s.full} for s in state.import_export],
        'slot_summary': state.summary,
        'raw_output': state.raw,
        'error': None,
    }


def _drive_status(drive_id: int) -> dict:
    """mt status for one drive, in the shape drive_status.html and the drive
    status endpoint read. Read through the non-rewinding node."""
    base = {'drive_id': drive_id, 'online': False, 'ready': False,
            'write_protected': False, 'bot': False, 'eot': False,
            'tape_loaded': False, 'block_size': None, 'density': None,
            'raw_output': ''}
    device = mapping.device_for_drive(int(drive_id))
    if not device:
        return {**base, 'success': False, 'device_path': '',
                'error': f'Could not find device for drive {drive_id}'}

    state = mt.status(device)
    return {
        **base,
        'success': True,
        'device_path': device,
        'online': state.online,
        'ready': state.ready,
        'write_protected': state.write_protected,
        'bot': state.at_bot,
        'eot': state.at_eot,
        'tape_loaded': state.has_medium,
        'block_size': state.block_size,
        'density': state.density_name,
        'raw_output': state.raw,
        'error': None,
    }


def _device_dict(device) -> dict:
    """An lsscsi device in the shape the discovery endpoint has returned."""
    address = device.address
    return {
        'host': f'[{address.host}:{address.channel}:{address.target}:{address.lun}]',
        'device_type': device.device_type,
        'vendor': device.vendor,
        'model': device.model,
        'revision': device.revision,
        'device_path': device.device_path,
        'generic_path': device.generic_path,
    }


def _existing_barcodes(library_id: int) -> list:
    """Every barcode in a library's slots, uppercase, read with sudo if needed."""
    return [barcode.upper() for barcode in _tapes().barcodes_in(int(library_id))
            if barcode]


def _barcode_in_slot(library_id: int, slot: int):
    """(barcode, None) for the tape in a slot, or (None, why not)."""
    contents = ConfigService().library_contents(int(library_id))
    if contents is None:
        return None, f'Could not read library {library_id}'
    found = next((s for s in contents.occupied if s.number == slot), None)
    if found is None:
        return None, f'Slot {slot} is empty'
    return found.barcode, None


def _delete_tape(library_id: int, barcode, slot, delete_data: bool):
    """Delete by barcode, or by the slot that holds it."""
    if not barcode and slot is not None:
        barcode, problem = _barcode_in_slot(library_id, slot)
        if problem:
            return failure_result(problem, [problem])
    return _tapes().delete(library_id, barcode, remove_media=delete_data)


def _slot_stats(library_id: int) -> dict:
    """Slot counts from library_contents, and the lowest empty slot."""
    contents = ConfigService().library_contents(int(library_id))
    if contents is None:
        return {'total': 0, 'occupied': 0, 'free': 0, 'next_slot': 1}
    occupied = len(contents.occupied)
    next_slot = next((slot.number for slot in sorted(contents.slots, key=lambda s: s.number)
                      if not slot.full), None)
    return {'total': len(contents.slots), 'occupied': occupied,
            'free': len(contents.slots) - occupied, 'next_slot': next_slot}


def _drives_of(library_id: int) -> list:
    """A library's drives from device.conf, as dicts (DriveService.list)."""
    result = DriveService().list(library_id=int(library_id))
    if not result.success:
        logger.warning('listing drives of library %s: %s', library_id, result.message)
        return []
    return result.data['drives']


def tape_media_context(libraries) -> dict:
    """What the two tape-creation pages need to offer only usable media.

    For each library, the densities its drives load (and which only read-
    only), from TapeService.media_for_library; plus every density that can be
    created, with its barcode suffixes and native capacity, for a library whose
    drives are unknown. The pages filter the density list by library with
    this, and the service refuses anything else anyway.
    """
    tapes = _tapes()
    library_media = {}
    for lib in libraries:
        try:
            library_media[lib['library_id']] = tapes.media_for_library(lib['library_id'])
        except Exception:                               # noqa: BLE001 - offer all
            logger.exception('reading the drives of library %s', lib['library_id'])
    return {
        'densities': [(d, personalities.media_label(d))
                      for d in personalities.SUFFIX_BY_DENSITY],
        'media_info': {
            'libraries': library_media,
            'suffix': personalities.SUFFIX_BY_DENSITY,
            'worm_suffix': personalities.WORM_SUFFIX_BY_DENSITY,
            'labels': {d: personalities.media_label(d)
                       for d in personalities.SUFFIX_BY_DENSITY},
            'native_mb': {d: gb * 1000 for d, gb
                          in personalities.NATIVE_CAPACITY_GB.items()},
        },
    }


# ============================================================================
# Operator Dashboard View
# ============================================================================

class OperatorDashboardView(View):
    """
    Main operator dashboard - entry point for tape operations
    Similar to vtlcmd.php in PHP GUI
    """
    template_name = 'libraries/operator/dashboard.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        # Get all active libraries
        libraries = get_live_libraries()

        context = {
            'libraries': libraries,
            'tape_service_available': TAPE_SERVICE_AVAILABLE,
            'title': 'Library Operator Panel'
        }

        return render(request, self.template_name, context)


# ============================================================================
# Library Status Views
# ============================================================================

def _picked(request, from_path, field: str):
    """The id the page is showing: the one in the URL, or the one the picker
    submitted.

    Both status pages are reachable as /library-status/20/ and as
    /library-status/?library_id=20, and their dropdown submits the second
    form - so a page that read only the URL showed nothing when the library
    was changed from the page itself.
    """
    if from_path:
        return from_path
    try:
        return int(request.GET.get(field, ''))
    except (TypeError, ValueError):
        return None


class LibraryStatusView(View):
    """
    Show library status (mtx status output)
    Similar to robot.status.php in PHP GUI
    """
    template_name = 'libraries/operator/library_status.html'

    def get(self, request, library_id=None):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = _picked(request, library_id, 'library_id')
        libraries = get_live_libraries()
        selected_library = None
        status_result = None

        if library_id:
            selected_library = next((lib for lib in libraries if lib['library_id'] == library_id), None)

            status_result = _library_status(library_id)

        context = {
            'libraries': libraries,
            'selected_library': selected_library,
            'status_result': status_result,
            'title': 'Library Status'
        }

        return render(request, self.template_name, context)


class DriveStatusView(View):
    """
    Show drive status (mt status output)
    Similar to drive.status.php in PHP GUI
    """
    template_name = 'libraries/operator/drive_status.html'

    def get(self, request, drive_id=None):
        if not check_login(request):
            return redirect('authentication:login')

        drive_id = _picked(request, drive_id, 'drive_id')
        # Get all drives
        drives = Drive.objects.filter(is_active=True).select_related('library')
        selected_drive = None
        status_result = None

        if drive_id:
            selected_drive = get_object_or_404(Drive, drive_id=drive_id, is_active=True)

            status_result = _drive_status(drive_id)

        context = {
            'drives': drives,
            'selected_drive': selected_drive,
            'status_result': status_result,
            'title': 'Drive Status'
        }

        return render(request, self.template_name, context)


# ============================================================================
# Tape Movement Views
# ============================================================================

class MountTapeView(View):
    """
    Mount tape from slot to drive
    Similar to form.mount.tape.php in PHP GUI
    """
    template_name = 'libraries/operator/mount_tape.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # Check for pre-selected library (from query param or session)
        selected_library_id = request.GET.get('library_id') or request.session.get('last_library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': int(selected_library_id) if selected_library_id else None,
            'title': 'Mount Tape'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        slot = request.POST.get('slot')
        drive = request.POST.get('drive')

        if not all([library_id, slot, drive]):
            messages.error(request, "Please select library, slot, and drive")
            return redirect('libraries:mount_tape')

        try:
            library_id = int(library_id)
            slot = int(slot)
            drive = int(drive)

            # Remember last used library
            request.session['last_library_id'] = library_id


            result = _operations().mount(library_id, slot, drive)

            if result.success:
                messages.success(request, result.message)
            else:
                messages.error(request, result.message)
                for error in result.errors:
                    messages.error(request, f"  {error}")

        except ValueError:
            messages.error(request, "Invalid slot or drive number")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        # Redirect back with library_id to preserve selection
        return redirect(f'/libraries/operator/mount/?library_id={library_id}')


class UnmountTapeView(View):
    """
    Unmount tape from drive to slot
    Similar to form.unmount.tape.php in PHP GUI
    """
    template_name = 'libraries/operator/unmount_tape.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # Check for pre-selected library (from query param or session)
        selected_library_id = request.GET.get('library_id') or request.session.get('last_library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': int(selected_library_id) if selected_library_id else None,
            'title': 'Unmount Tape'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        slot = request.POST.get('slot')
        drive = request.POST.get('drive')

        if not all([library_id, slot, drive]):
            messages.error(request, "Please select library, slot, and drive")
            return redirect('libraries:unmount_tape')

        try:
            library_id = int(library_id)
            slot = int(slot)
            drive = int(drive)

            # Remember last used library
            request.session['last_library_id'] = library_id


            result = _operations().unmount(library_id, drive, slot)

            if result.success:
                messages.success(request, result.message)
            else:
                messages.error(request, result.message)
                for error in result.errors:
                    messages.error(request, f"  {error}")

        except ValueError:
            messages.error(request, "Invalid slot or drive number")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        # Redirect back with library_id to preserve selection
        return redirect(f'/libraries/operator/unmount/?library_id={library_id}')


class MoveTapeView(View):
    """
    Move tape between slots
    Similar to form.move.tape.php in PHP GUI
    """
    template_name = 'libraries/operator/move_tape.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # Check for pre-selected library (from query param or session)
        selected_library_id = request.GET.get('library_id') or request.session.get('last_library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': int(selected_library_id) if selected_library_id else None,
            'title': 'Move Tape'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        from_slot = request.POST.get('from_slot')
        to_slot = request.POST.get('to_slot')

        if not all([library_id, from_slot, to_slot]):
            messages.error(request, "Please select library and both slots")
            return redirect('libraries:move_tape')

        try:
            library_id = int(library_id)
            from_slot = int(from_slot)
            to_slot = int(to_slot)

            # Remember last used library
            request.session['last_library_id'] = library_id

            if from_slot == to_slot:
                messages.error(request, "Source and destination slots must be different")
                return redirect(f'/libraries/operator/move/?library_id={library_id}')


            result = _operations().move(library_id, from_slot, to_slot)

            if result.success:
                messages.success(request, result.message)
            else:
                messages.error(request, result.message)
                for error in result.errors:
                    messages.error(request, f"  {error}")

        except ValueError:
            messages.error(request, "Invalid slot numbers")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        # Redirect back with library_id to preserve selection
        return redirect(f'/libraries/operator/move/?library_id={library_id}')


# ============================================================================
# Library Control Views
# ============================================================================

class LibraryOnlineView(View):
    """
    Set library online
    Similar to form.vtlcmd.online.php in PHP GUI
    """
    template_name = 'libraries/operator/library_online.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # The page is reached from a library's own page, which puts the
        # library in the link; without this the picker opened empty and
        # the operator had to choose it again.
        selected_library_id = _picked(request, None, 'library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'title': 'Set Library Online'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')

        if not library_id:
            messages.error(request, "Please select a library")
            return redirect('libraries:library_online')

        try:
            library_id = int(library_id)


            result = _operations().online(library_id)

            if result.success:
                messages.success(request, result.message)
            else:
                messages.error(request, result.message)

        except ValueError:
            messages.error(request, "Invalid library ID")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:library_online')


class LibraryOfflineView(View):
    """
    Set library offline
    Similar to form.vtlcmd.offline.php in PHP GUI
    """
    template_name = 'libraries/operator/library_offline.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # The page is reached from a library's own page, which puts the
        # library in the link; without this the picker opened empty and
        # the operator had to choose it again.
        selected_library_id = _picked(request, None, 'library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'title': 'Set Library Offline'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')

        if not library_id:
            messages.error(request, "Please select a library")
            return redirect('libraries:library_offline')

        try:
            library_id = int(library_id)


            result = _operations().offline(library_id)

            if result.success:
                messages.success(request, result.message)
            else:
                messages.error(request, result.message)

        except ValueError:
            messages.error(request, "Invalid library ID")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:library_offline')


# ============================================================================
# AJAX API Endpoints
# ============================================================================

def library_status_ajax(request, library_id):
    """
    AJAX endpoint for library status
    Returns JSON with slot and drive information
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        return JsonResponse(_library_status(library_id))
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def drive_status_ajax(request, drive_id):
    """
    AJAX endpoint for drive status
    Returns JSON with drive state
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        return JsonResponse(_drive_status(drive_id))
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def mount_tape_ajax(request):
    """
    AJAX endpoint for mounting tape
    POST with JSON body: {library_id, slot, drive}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        library_id = int(data.get('library_id'))
        slot = int(data.get('slot'))
        drive = int(data.get('drive'))

        result = _operations().mount(library_id, slot, drive)
        return JsonResponse(_as_json(result))

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def unmount_tape_ajax(request):
    """
    AJAX endpoint for unmounting tape
    POST with JSON body: {library_id, slot, drive}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        library_id = int(data.get('library_id'))
        slot = int(data.get('slot'))
        drive = int(data.get('drive'))

        result = _operations().unmount(library_id, drive, slot)
        return JsonResponse(_as_json(result))

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def move_tape_ajax(request):
    """
    AJAX endpoint for moving tape between slots
    POST with JSON body: {library_id, from_slot, to_slot}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        library_id = int(data.get('library_id'))
        from_slot = int(data.get('from_slot'))
        to_slot = int(data.get('to_slot'))

        result = _operations().move(library_id, from_slot, to_slot)
        return JsonResponse(_as_json(result))

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def discover_devices_ajax(request):
    """
    AJAX endpoint for discovering SCSI devices
    Returns lists of robots and tape drives
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        devices = lsscsi.discover()
        return JsonResponse({
            'success': True,
            'robots': [_device_dict(d) for d in devices if d.device_type == 'mediumx'],
            'tapes': [_device_dict(d) for d in devices if d.device_type == 'tape'],
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ============================================================================
# Tape Management Views (Create/Delete/List)
# ============================================================================

class TapeListView(View):
    """
    List all tapes in a library
    """
    template_name = 'libraries/operator/tape_list.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()
        selected_library = None
        tapes_result = None

        library_id = request.GET.get('library_id')
        if library_id:
            try:
                library_id = int(library_id)
                selected_library = next((lib for lib in libraries if lib['library_id'] == library_id), None)
                if not selected_library:
                    raise ValueError("Library not found")

                tapes_result = _tapes().list(library_id)
            except ValueError:
                messages.error(request, "Invalid library")

        tapes_data = tapes_result.data if tapes_result and tapes_result.success else None
        tapes_list = tapes_data['tapes'] if tapes_data else []

        # Tapes whose files are on disk that no library lists - what a delete
        # keeping the media leaves behind. Host-wide, so they show whether or
        # not a library is picked, and each can be adopted back.
        loose = _tapes().unassigned()

        context = {
            'libraries': libraries,
            'selected_library': selected_library,
            'tapes_result': tapes_data,
            'in_slot_count': sum(1 for t in tapes_list if t.get('location') == 'slot'),
            'in_drive_count': sum(1 for t in tapes_list if t.get('location') == 'drive'),
            'unassigned': loose.data['tapes'] if loose.success else [],
            'unassigned_error': None if loose.success else loose.message,
            'error': tapes_result.message if tapes_result and not tapes_result.success else None,
            'title': 'Tape Inventory'
        }

        return render(request, self.template_name, context)


class CreateTapeView(View):
    """
    Create a new tape
    """
    template_name = 'libraries/operator/create_tape.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # The barcode series each library uses (services/tapes): it was worked
        # out here, twice, with a copy of the profile lookup in each.
        library_prefixes = {lib['library_id']: _tapes().barcode_prefix(lib['library_id'])
                            for lib in libraries}


        tape_types = [
            ('data', 'Data Tape'),
            ('clean', 'Cleaning Tape'),
            ('WORM', 'WORM Tape'),
        ]

        # Only the media the chosen library's drives load are offered; the
        # list used to be fixed (LTO-1 to LTO-8, SDLT600, DLT4) whatever the
        # library held.
        # The page is reached from a library's own page, which puts the
        # library in the link; without this the picker opened empty and
        # the operator had to choose it again.
        selected_library_id = _picked(request, None, 'library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'library_prefixes': library_prefixes,
            'tape_types': tape_types,
            'title': 'Create Tape',
            **tape_media_context(libraries),
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        barcode = request.POST.get('barcode', '').strip().upper()
        slot = request.POST.get('slot')
        size_mb = request.POST.get('size_mb', '500000')
        tape_type = request.POST.get('tape_type', 'data')
        # No default: the service reads it from the barcode, then the library
        density = request.POST.get('density') or None

        if not all([library_id, barcode, slot]):
            messages.error(request, "Please fill in all required fields")
            return redirect('libraries:create_tape')

        try:
            library_id = int(library_id)
            slot = int(slot)
            size_mb = int(size_mb)


            result = _tapes().create(library_id, barcode, slot=slot, size_mb=size_mb,
                                     density=density, kind=tape_type)

            if result.success:
                messages.success(request, result.message)
                return redirect('libraries:tape_list')
            else:
                messages.error(request, result.message)

        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:create_tape')


class CreateTapesBulkView(View):
    """
    Create multiple tapes at once
    """
    template_name = 'libraries/operator/create_tapes_bulk.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # The barcode series each library uses (services/tapes): it was worked
        # out here, twice, with a copy of the profile lookup in each.
        library_prefixes = {lib['library_id']: _tapes().barcode_prefix(lib['library_id'])
                            for lib in libraries}


        tape_types = [
            ('data', 'Data Tape'),
            ('clean', 'Cleaning Tape'),
            ('WORM', 'WORM Tape'),
        ]

        # Only the media the chosen library's drives load are offered; the
        # list used to be fixed (LTO-1 to LTO-8, SDLT600, DLT4) whatever the
        # library held.
        # The page is reached from a library's own page, which puts the
        # library in the link; without this the picker opened empty and
        # the operator had to choose it again.
        selected_library_id = _picked(request, None, 'library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'library_prefixes': library_prefixes,
            'tape_types': tape_types,
            'title': 'Create Multiple Tapes',
            **tape_media_context(libraries),
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        count = request.POST.get('count', '10')
        barcode_prefix = request.POST.get('barcode_prefix') or None
        barcode_suffix = request.POST.get('barcode_suffix') or None
        size_mb = request.POST.get('size_mb', '500000')
        density = request.POST.get('density') or None
        tape_type = request.POST.get('tape_type', 'data')

        if not all([library_id, count]):
            messages.error(request, "Please fill in all required fields")
            return redirect('libraries:create_tapes_bulk')

        try:
            library_id = int(library_id)
            count = int(count)
            size_mb = int(size_mb)

            # Validate count based on tape type
            max_counts = {'data': 999, 'clean': 9, 'WORM': 99}
            max_count = max_counts.get(tape_type, 100)
            if count < 1 or count > max_count:
                messages.error(request, f"Count must be between 1 and {max_count} for {tape_type} tapes")
                return redirect('libraries:create_tapes_bulk')


            result = _tapes().create_bulk(library_id, count, prefix=barcode_prefix,
                                          suffix=barcode_suffix, size_mb=size_mb,
                                          density=density, kind=tape_type)

            if result.success:
                created_count = len(result.data.get('created', []))
                messages.success(request, f"Created {created_count} tapes successfully")
                return redirect('libraries:tape_list')
            else:
                messages.error(request, result.message)
                if result.data and result.data.get('created'):
                    messages.info(request, f"Partially created {len(result.data['created'])} tapes")

        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:create_tapes_bulk')


class DeleteTapeView(View):
    """
    Delete a tape from a library
    """
    template_name = 'libraries/operator/delete_tape.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()

        # The page is reached from a library's own page, which puts the
        # library in the link; without this the picker opened empty and
        # the operator had to choose it again.
        selected_library_id = _picked(request, None, 'library_id')

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'title': 'Delete Tape'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        barcode = request.POST.get('barcode', '').strip()
        slot = request.POST.get('slot')
        delete_data = request.POST.get('delete_data') == 'on'

        if not library_id or (not barcode and not slot):
            messages.error(request, "Please select a library and provide barcode or slot")
            return redirect('libraries:delete_tape')

        try:
            library_id = int(library_id)
            slot = int(slot) if slot else None


            result = _delete_tape(library_id, barcode or None, slot, delete_data)

            if result.success:
                messages.success(request, result.message)
                return redirect('libraries:tape_list')
            else:
                messages.error(request, result.message)

        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:delete_tape')


class AdoptTapeView(RedirectOnGet, View):
    """Give a tape whose files are on disk back to a library.

    Nothing is written to the media: the barcode goes into a free slot and the
    tape comes back with everything that was on it. The library has to be
    restarted before its robot reports the tape, which this offers to do.

    It is the tape inventory's form, not a page: a GET goes back there.
    """

    page = 'libraries:tape_list'

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        barcode = request.POST.get('barcode', '').strip().upper()
        library_id = request.POST.get('library_id')
        slot = request.POST.get('slot')
        restart = request.POST.get('restart') == 'on'

        if not barcode or not library_id:
            messages.error(request, 'Choose a tape and the library to put it in')
            return redirect('libraries:tape_list')

        try:
            library_id = int(library_id)
            slot = int(slot) if slot else None
        except ValueError:
            messages.error(request, 'Invalid library or slot')
            return redirect('libraries:tape_list')

        result = _tapes().adopt(library_id, barcode, slot=slot)
        if not result.success:
            messages.error(request, result.message)
            return redirect(f'{reverse("libraries:tape_list")}?library_id={library_id}')

        messages.success(request, result.message)
        if restart:
            outcome = units.restart_library(library_id)
            if outcome.get('ok'):
                messages.success(request, f'Restarted {outcome["restarted"]}')
            else:
                messages.warning(
                    request,
                    f'{barcode} is in the library, but {outcome["restarted"]} did '
                    f'not restart: {outcome.get("error") or "systemctl reported a failure"}')
        else:
            messages.info(
                request,
                'Restart the library for its robot to see it: '
                f'Library Status > Library {library_id}, or '
                f'`mhvtl service restart --library {library_id}`')
        return redirect(f'{reverse("libraries:tape_list")}?library_id={library_id}')


# ============================================================================
# Drive Management Views
# ============================================================================

def get_drive_service():
    """Get the drive service.

    Drive CRUD lives in services.drives, not on MHVTLLibraryService: the views
    called add_drive/remove_drive on that service for months and got an
    AttributeError every time, because the only implementations were in an
    unimported fork.
    """
    return DriveService()


class DriveListView(View):
    """
    List all drives in a library
    """
    template_name = 'libraries/operator/drive_list.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()
        selected_library = None
        drives_result = None

        library_id = request.GET.get('library_id')
        if library_id:
            try:
                library_id = int(library_id)
                selected_library = next((lib for lib in libraries if lib['library_id'] == library_id), None)

                drives = _drives_of(library_id)
                drives_result = {'count': len(drives), 'drives': drives}
            except ValueError:
                messages.error(request, "Invalid library")

        context = {
            'libraries': libraries,
            'selected_library': selected_library,
            'drives_result': drives_result,
            'error': None,
            'title': 'Drive Management'
        }

        return render(request, self.template_name, context)


def library_activity(request, library_id):
    """What this library's drives are doing, as the page shows it.

    The library page swaps this fragment in every few seconds
    (js/auto-refresh.js). The words and the sizes are decided by
    DriveService.activity(), not here and not in the browser, so
    `mhvtl status activity <library>` prints the same thing.

    ?format=json answers with the same data for anything that would rather
    read numbers than HTML.

    The counters come through MHVTL's message queue (`vtlcmd <drive> stats`,
    our patch 0004), so they keep answering while a backup holds the SCSI
    reservation and mt and mtx cannot.
    """
    if not check_login(request):
        if request.GET.get('format') == 'json':
            return JsonResponse({'success': False,
                                 'error': 'Authentication required'}, status=401)
        return HttpResponse('Authentication required', status=401)

    # The page polls; a reading it cannot compare with is worth less than a
    # quick answer, and the next poll will have one.
    result = get_drive_service().activity(library_id, settle=0)

    if request.GET.get('format') == 'json':
        return JsonResponse({'success': result.success, 'message': result.message,
                             'errors': result.errors, **(result.data or {})})

    return render(request, 'libraries/partials/_library_activity.html',
                  {'drives': (result.data or {}).get('drives', [])})


def drive_placement_ajax(request, library_id):
    """Where a new drive would land in this library, and what it may be.

    The Add Drive page asks as soon as a library is chosen, so the operator
    sees the slot, the drive id, the SCSI target and the serial before
    committing - and the models offered are the ones that library takes.
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Authentication required'},
                            status=401)

    result = get_drive_service().placement(library_id)
    return JsonResponse({'success': result.success, 'message': result.message,
                         'errors': result.errors, 'plan': result.data or {}})


class AddDriveView(View):
    """
    Add a new drive to a library
    """
    template_name = 'libraries/operator/add_drive.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()
        selected_library_id = _picked(request, None, 'library_id')

        # What the drive would be and where it would go. The page asks again
        # over AJAX whenever the library changes; this is the first answer, so
        # a page opened from a library's own page is already filled in.
        plan = {}
        if selected_library_id:
            plan = get_drive_service().placement(selected_library_id).data or {}

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'plan': plan,
            'title': 'Add Drive'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        vendor = request.POST.get('vendor', 'IBM')
        product = request.POST.get('product', 'ULT3580-TD8')
        serial = request.POST.get('serial', '').strip()

        if not library_id:
            messages.error(request, "Please select a library")
            return redirect('libraries:add_drive')

        try:
            library_id = int(library_id)


            drive_data = {
                'vendor': vendor,
                'product': product,
            }
            if serial:
                drive_data['serial'] = serial

            result = get_drive_service().add(library_id, drive_data)

            if result.success:
                messages.success(request, result.message)
                messages.warning(request, "Restart MHVTL services for changes to take effect")
                return redirect('libraries:drive_list')
            else:
                messages.error(request, result.message)

        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:add_drive')


class RemoveDriveView(View):
    """
    Remove a drive from a library
    """
    template_name = 'libraries/operator/remove_drive.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = get_live_libraries()
        selected_library_id = request.GET.get('library_id')
        drives = []

        if selected_library_id:
            try:
                selected_library_id = int(selected_library_id)
                drives = _drives_of(selected_library_id)
            except ValueError:
                selected_library_id = None

        context = {
            'libraries': libraries,
            'selected_library_id': selected_library_id,
            'drives': drives,
            'title': 'Remove Drive'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        drive_id = request.POST.get('drive_id')

        if not drive_id:
            messages.error(request, "Please select a drive")
            return redirect('libraries:remove_drive')

        try:
            drive_id = int(drive_id)


            result = get_drive_service().remove(drive_id)

            if result.success:
                messages.success(request, result.message)
                messages.warning(request, "Restart MHVTL services for changes to take effect")
                return redirect('libraries:drive_list')
            else:
                messages.error(request, result.message)

        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

        return redirect('libraries:remove_drive')


# ============================================================================
# Tape Management AJAX Endpoints
# ============================================================================

def list_tapes_ajax(request, library_id):
    """
    AJAX endpoint for listing tapes in a library
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        result = _tapes().list(library_id)
        return JsonResponse({
            'success': result.success,
            'message': result.message,
            'data': result.data,
            'errors': result.errors
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def create_tape_ajax(request):
    """
    AJAX endpoint for creating a tape
    POST with JSON body: {library_id, barcode, slot, size_mb, tape_type, density}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        library_id = int(data.get('library_id'))
        barcode = data.get('barcode', '').strip().upper()
        slot = int(data.get('slot'))
        size_mb = int(data.get('size_mb', 500000))
        tape_type = data.get('tape_type', 'data')
        # No default: the service reads it from the barcode, then the library
        density = data.get('density') or None

        result = _tapes().create(library_id, barcode, slot=slot, size_mb=size_mb,
                                 density=density, kind=tape_type)

        return JsonResponse({
            'success': result.success,
            'message': result.message,
            'data': result.data,
            'errors': result.errors
        })

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def delete_tape_ajax(request):
    """
    AJAX endpoint for deleting a tape
    POST with JSON body: {library_id, barcode or slot, delete_data}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        library_id = int(data.get('library_id'))
        barcode = data.get('barcode')
        slot = data.get('slot')
        if slot:
            slot = int(slot)
        delete_data = data.get('delete_data', False)

        result = _delete_tape(library_id, barcode, slot, delete_data)

        return JsonResponse({
            'success': result.success,
            'message': result.message,
            'data': result.data,
            'errors': result.errors
        })

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ============================================================================
# Drive Management AJAX Endpoints
# ============================================================================

def list_drives_ajax(request, library_id=None):
    """
    AJAX endpoint for listing drives
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        # Accept library_id from URL path or GET query param
        if not library_id:
            library_id = request.GET.get('library_id')
        if library_id:
            library_id = int(library_id)
        else:
            return JsonResponse({'success': False, 'error': 'library_id required'}, status=400)
        keys = ('drive_id', 'slot', 'vendor', 'product', 'serial',
                'channel', 'target', 'lun')
        drives = [{key: d.get(key) for key in keys} for d in _drives_of(library_id)]
        return JsonResponse({
            'success': True,
            'count': len(drives),
            'drives': drives,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def add_drive_ajax(request):
    """
    AJAX endpoint for adding a drive
    POST with JSON body: {library_id, vendor, product, serial}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        library_id = int(data.get('library_id'))

        drive_data = {
            'vendor': data.get('vendor', 'IBM'),
            'product': data.get('product', 'ULT3580-TD8'),
        }
        if data.get('serial'):
            drive_data['serial'] = data['serial']

        result = get_drive_service().add(library_id, drive_data)

        return JsonResponse({
            'success': result.success,
            'message': result.message,
            'data': result.data,
            'errors': result.errors
        })

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def remove_drive_ajax(request):
    """
    AJAX endpoint for removing a drive
    POST with JSON body: {drive_id}
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        drive_id = int(data.get('drive_id'))

        result = get_drive_service().remove(drive_id)

        return JsonResponse({
            'success': result.success,
            'message': result.message,
            'data': result.data,
            'errors': result.errors
        })

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ============================================================================
# Barcode Utility AJAX Endpoints
# ============================================================================

def density_suffix_mapping_ajax(request):
    """
    AJAX endpoint for getting density to barcode suffix mapping.
    GET - Returns the mapping dictionary.
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        return JsonResponse({
            'success': True,
            'mapping': dict(personalities.SUFFIX_BY_DENSITY),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def validate_barcode_ajax(request, library_id):
    """
    AJAX endpoint for validating barcode uniqueness.
    GET with query param: ?barcode=E01001L8
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        barcode = request.GET.get('barcode', '').strip().upper()
        if not barcode:
            return JsonResponse({
                'success': False,
                'error': 'Barcode parameter required'
            }, status=400)

        try:
            checked = barcodes.validate(barcode)
        except barcodes.InvalidBarcode as exc:
            valid, message = False, str(exc)
        else:
            if checked in _existing_barcodes(int(library_id)):
                valid = False
                message = f'Barcode "{checked}" already exists in library {library_id}'
            else:
                valid, message = True, 'Barcode is available'
        return JsonResponse({
            'success': True,
            'valid': valid,
            'message': message,
            'barcode': barcode
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def next_barcode_ajax(request, library_id):
    """
    AJAX endpoint for getting the next available barcode.
    GET with query params: ?prefix=E01&suffix=L8
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        prefix = request.GET.get('prefix', 'E01').strip().upper()
        suffix = request.GET.get('suffix', 'L8').strip().upper()

        # The number space is shared across tape generations: E01001L7 and
        # E01001L8 are two tapes with the same number (barcodes.used_numbers).
        existing = _existing_barcodes(int(library_id))
        number = barcodes.next_number(existing, prefix)
        return JsonResponse({
            'success': True,
            'detected_suffix': barcodes.detect_suffix(existing, suffix),
            'next_number': number,
            'next_barcode': f'{prefix}{number:03d}{suffix}',
            'existing_count': len(barcodes.used_numbers(existing, prefix)),
            'consecutive_available': barcodes.consecutive_free(existing, prefix, number),
            'prefix': prefix,
            'suffix': suffix,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def existing_barcodes_ajax(request, library_id):
    """
    AJAX endpoint for getting all existing barcodes in a library.
    GET - Returns list of barcodes.
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        existing = _existing_barcodes(int(library_id))
        return JsonResponse({
            'success': True,
            'library_id': int(library_id),
            'barcodes': existing,
            'count': len(existing)
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def next_slot_ajax(request, library_id):
    """
    AJAX endpoint for getting the next available slot.
    GET - Returns next available slot number.
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)

    try:
        stats = _slot_stats(int(library_id))
        return JsonResponse({
            'success': True,
            'library_id': int(library_id),
            'next_slot': stats['next_slot'],
            'available': stats['next_slot'] is not None,
            'total_slots': stats['total'],
            'occupied_slots': stats['occupied'],
            'free_slots': stats['free'],
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
