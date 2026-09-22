"""
Backup/Restore Test Views
Views for end-to-end library backup/restore verification testing.

Location: apps/libraries/backup_restore_test_views.py

The views call services directly: services/verification runs the test,
services/libraries and services/tapes list what can be tested, and
device.conf (through ConfigService) says which drives a library has. The
helpers below turn a form's drive index into a device.conf drive id and a
VerificationReport into the shape the templates read; they used to be
adapters/backup_restore_test_service.py.
"""

from django.shortcuts import render, redirect
from django.views import View
from .post_only import JsonOnGet
from django.contrib import messages
from django.http import JsonResponse
import json
import logging
import threading

from .models import Library
from .services.config.service import ConfigService
from .services.libraries import LibraryService
from .services.tapes import TapeService
from .services.verification import VerificationService

logger = logging.getLogger(__name__)

# Store running tests (in production, use Redis or database)
running_tests = {}

# -- helpers -------------------------------------------------------------------

def _drive_ids(library_id: int) -> list:
    """The library's device.conf drive ids, in order; the form posts an index
    into this list. The Django database is not asked: it can disagree with
    device.conf, and then the page would name one drive and test another."""
    conf = ConfigService().device_conf()
    if conf is None:
        logger.warning('cannot read device.conf for the drives of library %s',
                       library_id)
        return []
    return sorted(conf.drives_of(int(library_id)))


def _test_tapes(library_id: int) -> list:
    """The data tapes in a library's slots, for the form to pick one from.
    Cleaning cartridges are left out."""
    listed = TapeService().list(int(library_id), with_usage=False)
    if not listed.success:
        raise RuntimeError(listed.message)
    return [{'slot': tape['slot'], 'barcode': tape['barcode']}
            for tape in listed.data['tapes']
            if tape.get('slot') and tape.get('barcode')
            and not tape['barcode'].startswith('CLN')]


def _iscsi_devices(library_id: int, drive_id: int):
    """The library's changer and this drive as attached over iSCSI, or why not."""
    from .services.iscsi import initiator
    try:
        found = initiator.library_paths(library_id, drive_id)
    except Exception as exc:                           # noqa: BLE001 - reported
        return None, str(exc)
    if found is None:
        return None, (f'no iSCSI target exporting library {library_id}\'s changer '
                      f'and drive {drive_id} is attached on this host')
    return found, ''


def _run_test(library_id: int, slot: int, drive_index: int, *,
              size_mb: int, num_files: int, via_iscsi: bool = False) -> dict:
    """Run the verification and return it in the shape the pages read.

    The keys are the ones the results template and the status endpoint
    always used; summary now carries drive, num_files, steps_passed and
    steps_completed, which the template showed and nothing filled in.
    """
    drive_ids = _drive_ids(library_id)
    if not 0 <= drive_index < len(drive_ids):
        message = f'Library {library_id} has no drive at index {drive_index}'
        return {'success': False, 'message': message, 'test_id': '',
                'start_time': '', 'end_time': '', 'total_duration': 0.0,
                'steps': [{'step': 'resolve drive', 'success': False,
                           'message': f'no drive {drive_index} in device.conf',
                           'duration': 0.0, 'errors': [f'library {library_id}']}],
                'summary': None}

    devices = None
    if via_iscsi:
        devices, problem = _iscsi_devices(library_id, drive_ids[drive_index])
        if devices is None:
            return {'success': False, 'message': f'Not run over iSCSI: {problem}',
                    'test_id': '', 'start_time': '', 'end_time': '',
                    'total_duration': 0.0,
                    'steps': [{'step': 'find the iSCSI devices', 'success': False,
                               'message': problem, 'duration': 0.0,
                               'errors': ['attach the target from its iSCSI page']}],
                    'summary': None}

    report = VerificationService().report(
        library_id, slot, drive_ids[drive_index], size_mb=size_mb,
        file_count=num_files, cleanup_on_success=True, devices=devices)

    failed = report.failed_step
    message = ('Backup and restore verified' if report.success
               else f'Failed at "{failed.step}": {failed.message}'
               if failed else 'Verification failed')
    if devices:
        message += (f' over iSCSI ({devices["iqn"]}: changer {devices["changer"]}, '
                    f'drive {devices["drive"]})')
    summary = dict(report.summary or {})
    if summary:
        if devices:
            summary['iscsi_target'] = devices['iqn']
        summary.update({
            'drive': summary.get('drive_id'),
            'num_files': summary.get('file_count'),
            'steps_passed': sum(1 for step in report.steps if step.success),
            'steps_completed': len(report.steps),
        })
    return {
        'success': report.success,
        'message': message,
        'test_id': report.run_id,
        'start_time': report.started_at,
        'end_time': report.finished_at,
        'total_duration': round(report.duration_seconds, 2),
        'steps': [{'step': step.step, 'success': step.success,
                   'message': step.message,
                   'duration': round(step.duration_seconds, 2),
                   'errors': step.errors}
                  for step in report.steps],
        'summary': summary or None,
    }


def check_login(request):
    """Check if user is logged in"""
    return request.session.get('mhvtl_logged_in', False)


class BackupRestoreTestView(View):
    """
    Main backup/restore test interface.
    """
    template_name = 'libraries/operator/backup_restore_test.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = Library.objects.filter(is_active=True)

        # Live vendor/product from device.conf
        live_vendor_map = {}
        listed = LibraryService().list(with_contents=False)
        for mlib in (listed.data or {}).get('libraries', []):
            live_vendor_map[mlib['library_id']] = {'vendor': mlib['vendor'],
                                                   'product': mlib['product']}

        # Available tapes and drives for each library
        library_data = []
        for lib in libraries:
            live = live_vendor_map.get(lib.library_id, {})
            entry = {
                'library': lib,
                'vendor': live.get('vendor', lib.vendor_identification),
                'product': live.get('product', lib.product_identification),
                'tapes': [],
                'drive_count': 0,
            }
            try:
                entry['tapes'] = _test_tapes(lib.library_id)[:20]  # Limit for UI
                entry['drive_count'] = len(_drive_ids(lib.library_id))
            except Exception as e:
                logger.error(f"Failed to get tapes for library {lib.library_id}: {e}")
            library_data.append(entry)

        context = {
            'libraries': library_data,
            'service_available': True,
            'running_tests': running_tests,
            'title': 'Library Backup/Restore Test'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        # Get form data
        library_id = request.POST.get('library_id')
        slot = request.POST.get('slot')
        drive = request.POST.get('drive', '0')
        size_mb = request.POST.get('size_mb', '10')
        num_files = request.POST.get('num_files', '5')
        via_iscsi = request.POST.get('via_iscsi') == '1'

        if not all([library_id, slot]):
            messages.error(request, "Please select a library and tape slot")
            return redirect('libraries:backup_restore_test')

        try:
            library_id = int(library_id)
            slot = int(slot)
            drive = int(drive)
            size_mb = int(size_mb)
            num_files = int(num_files)

            # Validate inputs
            if size_mb < 1 or size_mb > 1000:
                messages.error(request, "Test size must be between 1 and 1000 MB")
                return redirect('libraries:backup_restore_test')

            if num_files < 1 or num_files > 100:
                messages.error(request, "Number of files must be between 1 and 100")
                return redirect('libraries:backup_restore_test')

            # Run the test
            result = _run_test(library_id, slot, drive,
                               size_mb=size_mb, num_files=num_files,
                               via_iscsi=via_iscsi)

            # Store result in session for display
            request.session['backup_restore_test_result'] = result

            if result['success']:
                messages.success(request, f"Test PASSED: {result['message']}")
            else:
                messages.error(request, f"Test FAILED: {result['message']}")

            return redirect('libraries:backup_restore_test_results')

        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
        except Exception as e:
            logger.error(f"Backup/restore test error: {e}")
            messages.error(request, f"Test error: {str(e)}")

        return redirect('libraries:backup_restore_test')


class BackupRestoreTestResultsView(View):
    """
    Display backup/restore test results.
    """
    template_name = 'libraries/operator/backup_restore_test_results.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        result = request.session.get('backup_restore_test_result')

        if not result:
            messages.warning(request, "No test results available")
            return redirect('libraries:backup_restore_test')

        context = {
            'result': result,
            'title': 'Backup/Restore Test Results'
        }

        return render(request, self.template_name, context)


class BackupRestoreTestRunAsyncView(JsonOnGet, View):
    """
    Run backup/restore test asynchronously (AJAX endpoint).
    """

    def post(self, request):
        if not check_login(request):
            return JsonResponse({'success': False, 'error': 'Not logged in'}, status=401)

        try:
            data = json.loads(request.body)
            library_id = int(data.get('library_id'))
            slot = int(data.get('slot'))
            drive = int(data.get('drive', 0))
            size_mb = int(data.get('size_mb', 10))
            num_files = int(data.get('num_files', 5))
            via_iscsi = bool(data.get('via_iscsi'))

            # Generate test ID
            import uuid
            test_id = str(uuid.uuid4())[:8]

            # Start test in background thread
            def run_test():
                try:
                    result = _run_test(library_id, slot, drive,
                                       size_mb=size_mb, num_files=num_files,
                                       via_iscsi=via_iscsi)
                except Exception as exc:  # noqa: BLE001 - reported to the poller
                    logger.exception('backup/restore test %s', test_id)
                    running_tests[test_id] = {
                        'status': 'completed',
                        'result': {'success': False, 'message': str(exc)}}
                    return
                running_tests[test_id] = {
                    'status': 'completed',
                    'result': {key: result[key] for key in (
                        'success', 'message', 'test_id', 'total_duration',
                        'summary')},
                }

            running_tests[test_id] = {'status': 'running'}
            thread = threading.Thread(target=run_test)
            thread.start()

            return JsonResponse({
                'success': True,
                'test_id': test_id,
                'message': 'Test started'
            })

        except Exception as e:
            logger.error(f"Failed to start async test: {e}")
            return JsonResponse({'success': False, 'error': str(e)}, status=500)


class BackupRestoreTestStatusView(View):
    """
    Check backup/restore test status (AJAX endpoint).
    """

    def get(self, request, test_id):
        if not check_login(request):
            return JsonResponse({'success': False, 'error': 'Not logged in'}, status=401)

        if test_id not in running_tests:
            return JsonResponse({'success': False, 'error': 'Test not found'}, status=404)

        test_info = running_tests[test_id]
        return JsonResponse({
            'success': True,
            'status': test_info.get('status'),
            'result': test_info.get('result')
        })


def get_library_tapes_ajax(request, library_id):
    """
    AJAX endpoint to get available tapes for a library.
    """
    if not check_login(request):
        return JsonResponse({'success': False, 'error': 'Not logged in'}, status=401)

    try:
        library_id = int(library_id)
        return JsonResponse({
            'success': True,
            'tapes': _test_tapes(library_id),
            'drives': [{'id': i, 'name': f'Drive {i}'}
                       for i in range(len(_drive_ids(library_id)))],
        })

    except Exception as e:
        logger.error(f"Failed to get tapes: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
