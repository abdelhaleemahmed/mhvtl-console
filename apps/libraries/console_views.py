# apps/libraries/console_views.py
"""
Console Views - System Information and Diagnostics

Provides views for the MHVTL Console module:
- System dashboard
- Log viewer
- Service status
- Kernel modules
- Disk usage

Every view calls services/console (and services/scsi for the device list)
directly. The helpers below only shape the answers into the names the console
templates and their JavaScript read; they used to live in
adapters/console_service.py.
"""

from django.shortcuts import render, redirect
from django.views import View
from django.http import JsonResponse
from django.contrib import messages

from .services.console import disk, logs, modules, system, units
from .services.core import config_dir
from .services.scsi import lsscsi


# -- shaping for the templates -------------------------------------------------

def _service_status() -> dict:
    """units.status(), under the names the console pages read."""
    state = units.status()

    def described(unit, key):
        # The template reads the instance number and sub_state, which
        # UnitState does not carry; the "ID:" beside each unit was always blank.
        return {**unit.to_dict(), key: units.instance_id(unit.name),
                'sub_state': unit.state}

    return {
        'target_active': state.target_active,
        'target_enabled': state.target_enabled,
        'target_status_text': 'active' if state.target_active else 'inactive',
        'modules_service_active': state.modules_loaded,
        'library_services': [described(unit, 'library_id') for unit in state.libraries],
        'library_services_count': len(state.libraries),
        'library_services_active': state.libraries_active,
        'tape_services': [described(unit, 'tape_id') for unit in state.drives],
        'tape_services_count': len(state.drives),
        'tape_services_active': state.drives_active,
        'all_services_healthy': state.healthy,
        'total_services': state.daemons_total,
        'active_services': state.daemons_active,
        'stale_services': state.stale,
    }


def _kernel_modules() -> list:
    return [{'name': name, **info} for name, info in modules.loaded().items()]


def _mhvtl_data() -> dict:
    usage = disk.media_usage()
    return {'data_dir': usage['path'], 'config_dir': str(config_dir()),
            'data_size': usage['size'] or 'Unknown',
            'tape_count': usage['tape_count'], 'details': []}


def _mhvtl_devices() -> list:
    devices = lsscsi.discover()
    return lsscsi.changers(devices) + lsscsi.tapes(devices)


def _with_content(result: dict) -> dict:
    """A log answer with its lines joined, which the log template shows."""
    return {**result, 'content': '\n'.join(result.get('lines', []))}


def _control(action: str) -> dict:
    run = {'start': units.start, 'stop': units.stop, 'restart': units.restart}.get(action)
    if run is None:
        return {'success': False, 'error': f'Unknown action: {action}'}
    outcome = run()
    return {'success': outcome.ok, 'error': outcome.output.strip()[:300]}


def _summary() -> dict:
    return {
        'system_info': system.info(),
        'mhvtl_service': _service_status(),
        'kernel_modules': _kernel_modules(),
        'disk_usage': disk.usage(),
        'mhvtl_data': _mhvtl_data(),
        'scsi_devices': _mhvtl_devices(),
    }


class ConsoleDashboardView(View):
    """Main console dashboard with system overview"""
    template_name = 'libraries/console/dashboard.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        try:
            summary = _summary()

            context = {
                'title': 'System Console',
                'system_info': summary['system_info'],
                'mhvtl_service': summary['mhvtl_service'],
                'kernel_modules': summary['kernel_modules'],
                'disk_usage': summary['disk_usage'],
                'mhvtl_data': summary['mhvtl_data'],
                'scsi_devices': summary['scsi_devices'],
            }
        except Exception as e:
            context = {
                'title': 'System Console',
                'error_message': f'Error loading system information: {str(e)}'
            }

        return render(request, self.template_name, context)


class LogViewerView(View):
    """View system and MHVTL logs"""
    template_name = 'libraries/console/logs.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        log_type = request.GET.get('type', 'mhvtl')
        lines = int(request.GET.get('lines', 100))

        try:
            if log_type == 'dmesg':
                log_data = _with_content(logs.dmesg(lines))
                log_title = 'Kernel Ring Buffer (dmesg)'
            elif log_type == 'system':
                log_data = _with_content(logs.read('/var/log/messages', lines))
                log_title = 'System Log (/var/log/messages)'
            else:
                log_data = _with_content(logs.read(None, lines))
                log_title = 'MHVTL Log'

            context = {
                'title': 'Log Viewer',
                'log_title': log_title,
                'log_type': log_type,
                'log_data': log_data,
                'lines': lines,
                'available_types': [
                    {'value': 'mhvtl', 'label': 'MHVTL Logs'},
                    {'value': 'system', 'label': 'System Log'},
                    {'value': 'dmesg', 'label': 'Kernel Messages'},
                ]
            }
        except Exception as e:
            context = {
                'title': 'Log Viewer',
                'error_message': f'Error loading logs: {str(e)}'
            }

        return render(request, self.template_name, context)


class ServiceStatusView(View):
    """View MHVTL service status"""
    template_name = 'libraries/console/service_status.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        try:
            service_status = _service_status()
            kernel_modules = modules.summary()

            context = {
                'title': 'MHVTL Service Status',
                'service': service_status,
                'kernel_modules': kernel_modules,
            }
        except Exception as e:
            context = {
                'title': 'MHVTL Service Status',
                'error_message': f'Error loading service status: {str(e)}'
            }

        return render(request, self.template_name, context)

    def post(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        action = request.POST.get('action')

        # Service control actions via mhvtl.target
        if action in ['start', 'stop', 'restart']:
            try:
                result = _control(action)
                if result['success']:
                    note = ''
                    if action in ('start', 'restart'):
                        # Every daemon made new devices; every iSCSI export
                        # still holds the old ones until it is rebound.
                        from .services.iscsi import bindings as iscsi_bindings
                        note = iscsi_bindings.after_restart(None)
                    messages.success(request, f'MHVTL target {action} command executed '
                                              f'successfully' + (f'; {note}' if note else ''))
                else:
                    messages.error(request, f'Failed to {action} MHVTL: {result.get("error", "Unknown error")}')
            except Exception as e:
                messages.error(request, f'Error: {str(e)}')

        elif action == 'restart_library':
            # One library's daemon, and its exports rebound - the others keep
            # running, which "Restart All" does not allow.
            from .services.libraries import LibraryService
            try:
                library_id = int(request.POST.get('library_id', ''))
            except ValueError:
                messages.error(request, 'Choose a library to restart')
                return redirect('libraries:console_service_status')
            result = LibraryService().restart_services(library_id)
            if result.success:
                messages.success(request, result.message)
            else:
                messages.error(request, f"{result.message}: {'; '.join(result.errors)}")

        return redirect('libraries:console_service_status')


class KernelModulesView(View):
    """View kernel modules status"""
    template_name = 'libraries/console/kernel_modules.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        try:
            loaded = _kernel_modules()
            module_check = modules.summary()

            context = {
                'title': 'Kernel Modules',
                'modules': loaded,
                'module_check': module_check,
            }
        except Exception as e:
            context = {
                'title': 'Kernel Modules',
                'error_message': f'Error loading kernel modules: {str(e)}'
            }

        return render(request, self.template_name, context)


class DiskUsageView(View):
    """View disk usage information"""
    template_name = 'libraries/console/disk_usage.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        try:
            disk_usage = disk.usage()
            mhvtl_data = _mhvtl_data()

            context = {
                'title': 'Disk Usage',
                'disk_usage': disk_usage,
                'mhvtl_data': mhvtl_data,
            }
        except Exception as e:
            context = {
                'title': 'Disk Usage',
                'error_message': f'Error loading disk usage: {str(e)}'
            }

        return render(request, self.template_name, context)


class ScsiDevicesView(View):
    """View SCSI devices"""
    template_name = 'libraries/console/scsi_devices.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        show_all = request.GET.get('all', 'false').lower() == 'true'

        try:
            if show_all:
                devices = lsscsi.discover()
            else:
                devices = _mhvtl_devices()

            context = {
                'title': 'SCSI Devices',
                'devices': devices,
                'show_all': show_all,
                'device_count': len(devices),
            }
        except Exception as e:
            context = {
                'title': 'SCSI Devices',
                'error_message': f'Error loading SCSI devices: {str(e)}'
            }

        return render(request, self.template_name, context)


# AJAX endpoints for real-time updates

def console_refresh_ajax(request):
    """AJAX endpoint to refresh console data"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        summary = _summary()
        mhvtl_service = summary['mhvtl_service']

        return JsonResponse({
            'success': True,
            'data': {
                'system_info': {
                    'hostname': summary['system_info'].hostname,
                    'uptime': summary['system_info'].uptime,
                    'load_average': summary['system_info'].load_average,
                    'memory_percent': summary['system_info'].memory_percent,
                    'memory_level': summary['system_info'].memory_level,
                },
                'mhvtl_service': {key: mhvtl_service[key] for key in (
                    'target_active', 'target_enabled', 'modules_service_active',
                    'library_services_count', 'library_services_active',
                    'tape_services_count', 'tape_services_active',
                    'all_services_healthy', 'total_services', 'active_services')},
                'kernel_modules': summary['kernel_modules'],
                'scsi_device_count': len(summary['scsi_devices']),
            }
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


def service_status_ajax(request):
    """AJAX endpoint for service status"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        status = _service_status()
        status.pop('stale_services')

        return JsonResponse({'success': True, 'service': status})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })
