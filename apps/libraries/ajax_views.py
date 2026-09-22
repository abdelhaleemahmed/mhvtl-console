# apps/libraries/ajax_views.py - Complete Updated Version with Script Service Integration
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views import View
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import timedelta
import json
import logging
import os
from pathlib import Path

from .models import Library, LibraryBrand, LibraryModel, Drive, LibraryOperation

# Every endpoint here calls apps/libraries/services directly. They used to go
# through adapters/mhvtl_script_service.py and mhvtl_library_service.py.
from .services.config.service import ConfigService
from .services.console.system import mhvtl_installation
from .services.core import home_dir
from .services.libraries import LibraryService, lifecycle
from .services.operations import mounting
from .services.operations.service import OperationsService


logger = logging.getLogger(__name__)


def _sync_database():
    """Bring the database in line with device.conf: (synced, error)."""
    from .services.sync.service import sync_mhvtl_to_django

    try:
        sync_mhvtl_to_django()
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.warning('syncing the database with device.conf: %s', exc)
        return False, str(exc)
    return True, None


def _config_check(config_dir=None, media_dir=None):
    """(valid, issues): the configuration parses and is self-consistent, and
    the media directory exists."""
    media_dir = Path(media_dir or home_dir())
    issues = []
    if not media_dir.exists():
        issues.append(f'the media directory {media_dir} does not exist')
    issues.extend(ConfigService(config_dir).validate().errors)
    return not issues, issues


def _configured_libraries():
    """device.conf's libraries as dicts (library_id, vendor, product, drives, ...)."""
    listed = LibraryService().list(with_contents=False)
    return (listed.data or {}).get('libraries', []) if listed.success else []


def _spec_for(library):
    """A creation spec for a library the database knows.

    The profile is the brand; the endpoints that built this used to leave it
    out, and the library rules refuse a specification without one.
    """
    model = library.model.name if library.model else None
    spec = {'library_id': library.library_id,
            'profile': library.brand.name.upper() if library.brand else 'STK',
            'serial': getattr(library, 'unit_serial_number', None)
            or f'XYZZY_{library.library_id}'}
    if model:
        spec.update(library_model=model, product=model)
    return spec


# =============================================================================
# API Views for Library Data
# =============================================================================

class LibraryModelsAPIView(View):
    """Get models for a specific brand (AJAX)"""

    def get(self, request, brand_id):
        try:
            models = LibraryModel.objects.filter(
                brand_id=brand_id,
                is_active=True
            ).values('id', 'name', 'product_identification', 'default_revision')

            return JsonResponse({
                'success': True,
                'models': list(models)
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            })


class LibraryStatusAPIView(View):
    """Get library status information (AJAX) with discovery data and system metrics"""

    def _get_system_metrics(self, library_id):
        """Get system metrics for the library (CPU, memory, disk, uptime)"""
        import subprocess
        from datetime import datetime

        metrics = {
            'cpu': None,
            'memory': None,
            'disk': None,
            'uptime': None
        }

        try:
            # Get CPU usage of vtllibrary process for this library
            try:
                result = subprocess.run(
                    ['pgrep', '-f', f'vtllibrary.*{library_id}'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    pid = result.stdout.strip().split()[0]
                    ps_result = subprocess.run(
                        ['ps', '-p', pid, '-o', '%cpu', '--no-headers'],
                        capture_output=True, text=True, timeout=5
                    )
                    if ps_result.returncode == 0:
                        metrics['cpu'] = round(float(ps_result.stdout.strip()), 1)
            except Exception:
                metrics['cpu'] = 0

            # Get memory usage (system-wide)
            try:
                result = subprocess.run(
                    ['free', '-m'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')
                    if len(lines) >= 2:
                        mem_parts = lines[1].split()
                        if len(mem_parts) >= 3:
                            total = int(mem_parts[1])
                            used = int(mem_parts[2])
                            if total > 0:
                                metrics['memory'] = round((used / total) * 100, 1)
            except Exception:
                pass

            # Get disk usage of /opt/mhvtl
            try:
                result = subprocess.run(
                    ['df', '-h', '/opt/mhvtl'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        if len(parts) >= 5:
                            pct = parts[4].replace('%', '')
                            metrics['disk'] = int(pct)
            except Exception:
                pass

            # Get uptime of vtllibrary service
            try:
                result = subprocess.run(
                    ['systemctl', 'show', f'vtllibrary@{library_id}.service',
                     '--property=ActiveEnterTimestamp', '--value'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    timestamp_str = result.stdout.strip()
                    if timestamp_str and timestamp_str not in ('n/a', ''):
                        try:
                            # Parse systemd timestamp (e.g., "Mon 2026-01-26 10:30:00 UTC")
                            start_time = datetime.strptime(
                                ' '.join(timestamp_str.split()[:4]),
                                '%a %Y-%m-%d %H:%M:%S'
                            )
                            delta = datetime.now() - start_time
                            days = delta.days
                            hours, remainder = divmod(delta.seconds, 3600)
                            minutes, _ = divmod(remainder, 60)
                            if days > 0:
                                metrics['uptime'] = f"{days}d {hours}h"
                            elif hours > 0:
                                metrics['uptime'] = f"{hours}h {minutes}m"
                            else:
                                metrics['uptime'] = f"{minutes}m"
                        except ValueError:
                            metrics['uptime'] = 'Running'
                    else:
                        metrics['uptime'] = 'Stopped'
            except Exception:
                metrics['uptime'] = 'Unknown'

        except Exception:
            pass

        return metrics

    def get(self, request, library_id):
        try:
            library = get_object_or_404(Library, library_id=library_id, is_active=True)

            # Get system metrics
            metrics = self._get_system_metrics(library_id)

            # Get drive status
            drives = library.drives.filter(is_active=True)
            drive_status = {
                'online': drives.count(),
                'total': drives.count(),
            }

            data = {
                'success': True,
                'library': {
                    'id': library.library_id,
                    'brand': library.brand.display_name if library.brand else library.vendor_identification,
                    'model': library.model.name if library.model else library.product_identification,
                    'total_slots': library.total_slots,
                    'media_count': library.media_count,
                    'empty_slots': library.empty_slots,
                    'drives_count': library.drives.filter(is_active=True).count(),
                    'is_active': library.is_active,
                    'discovery_status': getattr(library, 'discovery_status', 'django_created'),
                    'mhvtl_id': getattr(library, 'mhvtl_id', None),
                    'last_synced': getattr(library, 'last_synced', None),
                    'config_source': getattr(library, 'config_source', None),
                },
                'metrics': metrics,
                'drive_status': drive_status,
                'drives': [
                    {'id': d.id, 'drive_id': d.drive_id, 'status': 'Online'}
                    for d in drives
                ],
                'error_count': 0
            }

            if data['library']['last_synced']:
                data['library']['last_synced'] = library.last_synced.isoformat()
                data['library']['last_synced_display'] = f"{library.last_synced.strftime('%b %d, %Y %H:%M')}"

            return JsonResponse(data)

        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            })


# =============================================================================
# AJAX Endpoints
# =============================================================================

def library_status_detail(request, library_id):
    """AJAX endpoint to get detailed library status for auto-refresh"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        library = get_object_or_404(Library, library_id=library_id, is_active=True)

        data = {
            'success': True,
            'library_id': library.library_id,
            'discovery_status': getattr(library, 'discovery_status', 'django_created'),
            'last_synced': getattr(library, 'last_synced', None),
            'mhvtl_id': getattr(library, 'mhvtl_id', None),
            'config_source': getattr(library, 'config_source', None),
            'is_active': library.is_active,
            'drives_count': library.drives.filter(is_active=True).count(),
            'recent_operations_count': library.operations.filter(
                timestamp__gte=timezone.now() - timedelta(hours=24)
            ).count(),
        }

        if data['last_synced']:
            data['last_synced'] = library.last_synced.isoformat()
            data['last_synced_display'] = library.last_synced.strftime('%b %d, %Y %H:%M')

        return JsonResponse(data)

    except Library.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Library not found'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


# =============================================================================
# NEW: MHVTL Script Integration AJAX Endpoints
# =============================================================================

@require_POST
def create_library_ajax(request):
    """AJAX endpoint to create a library (services/libraries)."""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        data = json.loads(request.body)

        # Validate required fields
        required_fields = ['library_id', 'vendor', 'product']
        for field in required_fields:
            if field not in data:
                return JsonResponse({
                    'success': False,
                    'error': f'Missing required field: {field}'
                })

        result = LibraryService().create(data)

        # Handle ServiceResult object
        if result.success:
            # Bring the database in line with the configuration just written.
            # This called a discovery app that does not exist, so the answer
            # always said the sync had not run.
            synced, sync_error = _sync_database()

            # Log the operation
            try:
                library = Library.objects.filter(library_id=data['library_id']).first()
                if library:
                    LibraryOperation.objects.create(
                        library=library,
                        operation='CREATE',
                        description=f'Library {data["library_id"]} created via AJAX',
                        user_session=request.session.session_key or 'unknown'
                    )
            except Exception as log_error:
                logger.warning(f"Failed to log operation: {log_error}")

            return JsonResponse({
                'success': True,
                'message': result.message,
                'operation_id': result.operation_id,
                'data': result.data,
                'database_synced': synced,
                'database_error': sync_error
            })
        else:
            # Return error details from ServiceResult
            return JsonResponse({
                'success': False,
                'error': result.message,
                'errors': result.errors,
                'operation_id': result.operation_id
            })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'})
    except Exception as e:
        logger.error(f"Error creating library: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': f'Unexpected error: {str(e)}'
        })


@require_POST
def update_library_ajax(request, library_id):
    """AJAX endpoint to update a library (services/libraries)."""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        # Verify library exists in Django database
        try:
            library = Library.objects.get(library_id=library_id, is_active=True)
        except Library.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': f'Library {library_id} not found or inactive'
            })

        data = json.loads(request.body)
        data['library_id'] = library_id  # Ensure library_id is set

        result = LibraryService().update(library_id, data)

        # Handle ServiceResult object
        if result.success:
            # Update Django model with new values
            try:
                if 'vendor' in data:
                    library.vendor_identification = data['vendor'].upper()
                if 'product' in data:
                    library.product_identification = data['product']
                if 'serial' in data:
                    library.unit_serial_number = data['serial']
                library.save()
            except Exception as db_error:
                logger.warning(f"Failed to update Django model: {db_error}")

            # Bring the database in line with the configuration just written.
            # This called a discovery app that does not exist, so the answer
            # always said the sync had not run.
            synced, sync_error = _sync_database()

            # Log the operation
            try:
                LibraryOperation.objects.create(
                    library=library,
                    operation='UPDATE',
                    description=f'Library {library_id} updated via AJAX',
                    user_session=request.session.session_key or 'unknown'
                )
            except Exception as log_error:
                logger.warning(f"Failed to log operation: {log_error}")

            return JsonResponse({
                'success': True,
                'message': result.message,
                'operation_id': result.operation_id,
                'data': result.data,
                'database_synced': synced,
                'database_error': sync_error
            })
        else:
            # Return error details from ServiceResult
            return JsonResponse({
                'success': False,
                'error': result.message,
                'errors': result.errors,
                'operation_id': result.operation_id
            })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'})
    except Exception as e:
        logger.error(f"Error updating library {library_id}: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': f'Unexpected error: {str(e)}'
        })


@require_POST
def delete_library_ajax(request, library_id):
    """AJAX endpoint to delete a library (services/libraries)."""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        # Verify library exists in Django database
        try:
            library = Library.objects.get(library_id=library_id, is_active=True)
        except Library.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': f'Library {library_id} not found or already deleted'
            })

        # Check if force delete is requested
        import json
        try:
            body = json.loads(request.body) if request.body else {}
            force = body.get('force', False)
        except json.JSONDecodeError:
            force = False

        result = LibraryService().delete(library_id, force=force)

        # Handle ServiceResult object
        if result.success:
            # Mark library as inactive in Django
            library.is_active = False
            library.save()

            # Mark associated drives as inactive
            library.drives.update(is_active=False)

            # Log the operation
            try:
                LibraryOperation.objects.create(
                    library=library,
                    operation='DELETE',
                    description=f'Library {library_id} deleted via AJAX',
                    user_session=request.session.session_key or 'unknown'
                )
            except Exception as log_error:
                logger.warning(f"Failed to log operation: {log_error}")

            return JsonResponse({
                'success': True,
                'message': result.message,
                'operation_id': result.operation_id,
                'data': result.data,
                'django_deleted': True
            })
        else:
            # Return error details from ServiceResult
            response_data = {
                'success': False,
                'error': result.message,
                'errors': result.errors,
                'operation_id': result.operation_id
            }
            # Include safety check info if available (tapes mounted in drives)
            if result.data:
                if result.data.get('requires_force'):
                    response_data['requires_force'] = True
                if result.data.get('loaded_drives'):
                    response_data['loaded_drives'] = result.data['loaded_drives']
            return JsonResponse(response_data)

    except Exception as e:
        logger.error(f"Error deleting library {library_id}: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': f'Unexpected error: {str(e)}'
        })


def mhvtl_system_status_ajax(request):
    """AJAX endpoint to get comprehensive MHVTL system health status"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        import subprocess
        import shutil

        mhvtl_status = mhvtl_installation()
        config_valid, config_issues = _config_check()

        mhvtl_libraries = _configured_libraries()
        total_drives = sum(lib['drives'] for lib in mhvtl_libraries)

        # Service status
        services = {}
        for svc_name in ['mhvtl.target', 'mhvtl-gui', 'target']:
            try:
                result = subprocess.run(
                    ['systemctl', 'is-active', svc_name],
                    capture_output=True, text=True, timeout=5
                )
                services[svc_name] = result.stdout.strip()
            except Exception:
                services[svc_name] = 'unknown'

        # Kernel modules
        modules = {}
        for mod in ['mhvtl', 'target_core_mod', 'iscsi_target_mod', 'target_core_pscsi']:
            try:
                result = subprocess.run(
                    ['lsmod'], capture_output=True, text=True, timeout=5
                )
                modules[mod] = mod in result.stdout
            except Exception:
                modules[mod] = False

        # Disk usage for MHVTL directories
        disk_usage = {}
        from apps.libraries.services.core import config_dir as _config_dir
        from apps.libraries.services.core import home_dir as _home_dir
        for path in (str(_config_dir()), str(_home_dir())):
            path_name = path
            try:
                if os.path.exists(path):
                    usage = shutil.disk_usage(path)
                    disk_usage[path_name] = {
                        'total_gb': round(usage.total / (1024**3), 1),
                        'used_gb': round(usage.used / (1024**3), 1),
                        'free_gb': round(usage.free / (1024**3), 1),
                        'percent': round(usage.used / usage.total * 100, 1),
                    }
            except Exception:
                pass

        # Config file count and sizes
        config_dir = str(_config_dir())
        config_files = []
        if os.path.isdir(config_dir):
            for f in os.listdir(config_dir):
                fp = os.path.join(config_dir, f)
                if os.path.isfile(fp):
                    config_files.append({'name': f, 'size': os.path.getsize(fp)})

        # Django DB counts
        db_libraries = Library.objects.filter(is_active=True).count()
        db_drives = Drive.objects.filter(is_active=True).count()

        return JsonResponse({
            'success': True,
            'mhvtl_available': mhvtl_status['available'],
            'mhvtl_errors': mhvtl_status.get('errors', []),
            'scripts_status': mhvtl_status['scripts'],
            'config_valid': config_valid,
            'config_issues': config_issues,
            'config_dir_exists': mhvtl_status['config_dir_exists'],
            'data_dir_exists': mhvtl_status['data_dir_exists'],
            'libraries_in_mhvtl': len(mhvtl_libraries),
            'drives_in_mhvtl': total_drives,
            'libraries_in_db': db_libraries,
            'drives_in_db': db_drives,
            'services': services,
            'kernel_modules': modules,
            'disk_usage': disk_usage,
            'config_files': config_files,
        })

    except Exception as e:
        logger.error(f"Error checking MHVTL status: {str(e)}")
        return JsonResponse({'success': False, 'error': str(e)})


def preview_config_file_ajax(request):
    """AJAX endpoint to preview a config file's content"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    filename = request.GET.get('filename', '')

    # Security: only allow known config files
    allowed_files = ['device.conf', 'mhvtl.conf']
    allowed_patterns = ['library_contents.']

    if filename not in allowed_files and not any(filename.startswith(p) for p in allowed_patterns):
        return JsonResponse({'success': False, 'error': 'File not allowed'})

    # Read through the config service: it applies the same allowlist, uses the
    # configured directory rather than a hardcoded one, and falls back to sudo
    # on a file this process cannot open. This used a plain open() on
    # /etc/mhvtl, which failed on the stock 0600 device.conf.
    from apps.libraries.services.config.service import ConfigService

    content = ConfigService().read_file(filename)
    if content is None:
        return JsonResponse({'success': False, 'error': 'File not found'})

    return JsonResponse({
        'success': True,
        'filename': filename,
        'content': content,
        'size': len(content.encode()),
    })


@require_POST
def validate_mhvtl_config_ajax(request):
    """AJAX endpoint to validate MHVTL configuration"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})
    
    try:
        config_valid, config_issues = _config_check()

        return JsonResponse({
            'success': True,
            'config_valid': config_valid,
            'issues': config_issues,
            'timestamp': timezone.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error validating MHVTL config: {str(e)}")
        return JsonResponse({'success': False, 'error': str(e)})


@require_POST
def regenerate_configs_ajax(request):
    """AJAX endpoint to regenerate all MHVTL configuration files"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})
    
    try:
        # library_contents only; device.conf is the source it is written from
        regenerated = ConfigService().regenerate_library_contents(force=True)
        success = regenerated.success
        message = (regenerated.message if success
                   else '; '.join(regenerated.errors) or regenerated.message)
        
        if success:
            synced, sync_error = _sync_database()
            return JsonResponse({'success': True, 'message': message,
                                 'database_synced': synced,
                                 'database_error': sync_error})
        else:
            return JsonResponse({'success': False, 'error': message})
            
    except Exception as e:
        logger.error(f"Error regenerating configs: {str(e)}")
        return JsonResponse({'success': False, 'error': str(e)})


def get_library_status_mhvtl_ajax(request, library_id):
    """AJAX endpoint to get library status directly from MHVTL"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})
    
    try:
        result = OperationsService().status(int(library_id))
        if not result.success:
            return JsonResponse({'success': False, 'library_id': library_id,
                                 'error': result.message})
        return JsonResponse({'success': True, 'library_id': library_id,
                             'status': result.data})
        
    except Exception as e:
        logger.error(f"Error getting library status from MHVTL: {str(e)}")
        return JsonResponse({
            'success': False,
            'library_id': library_id,
            'error': str(e)
        })


# =============================================================================
# UPDATED: Legacy AJAX Endpoints (Modified for Script Service)
# =============================================================================

@require_POST
def export_library_ajax(request, library_id):
    """AJAX endpoint to export Django-created library to MHVTL (UPDATED for scripts)"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        library = get_object_or_404(Library, library_id=library_id, is_active=True)

        if getattr(library, 'discovery_status', 'django_created') != 'django_created':
            return JsonResponse({
                'success': False,
                'error': 'Only Django-created libraries can be exported to MHVTL'
            })

        try:
            created = LibraryService().create(_spec_for(library))
            success = created.success
            message = (created.message if success
                       else '; '.join(created.errors) or created.message)
            
            if success:
                library.config_generated = True
                library.last_synced = timezone.now()
                library.save()

                LibraryOperation.objects.create(
                    library=library,
                    operation='EXPORT',
                    description=f'Exported library {library_id} configuration to MHVTL via scripts',
                    user_session=request.session.session_key or 'unknown'
                )

                return JsonResponse({
                    'success': True,
                    'message': f'Library {library_id} exported to MHVTL successfully',
                    'method': 'mhvtl_scripts'
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': f'Export failed: {message}'
                })

        except Exception as e:
            logger.error(f"Script execution failed during export: {e}")
            return JsonResponse({
                'success': False,
                'error': f'Export failed: {str(e)}'
            })

    except Library.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Library not found'
        })
    except Exception as e:
        logger.error(f"Library export failed: {e}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@require_POST
def validate_config_ajax(request, library_id):
    """AJAX endpoint to validate library configuration (UPDATED for scripts)"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        library = get_object_or_404(Library, library_id=library_id, is_active=True)

        validation_issues = []

        # Basic library validation
        if not library.brand:
            validation_issues.append("Library brand is required")

        if not library.model:
            validation_issues.append("Library model is required")

        if library.total_slots <= 0:
            validation_issues.append("Total slots must be greater than 0")

        config_valid = None
        try:
            config_valid, config_issues = _config_check()

            if not config_valid:
                validation_issues.extend([f"MHVTL: {issue}" for issue in config_issues])
        
        except Exception as e:
            validation_issues.append(f"MHVTL validation failed: {str(e)}")

        # Check for conflicts
        conflicts = Library.objects.filter(
            is_active=True,
            channel=getattr(library, 'channel', 0),
            target=getattr(library, 'target', 0),
            lun=getattr(library, 'lun', 0)
        ).exclude(library_id=library_id)

        if conflicts.exists():
            validation_issues.append(f"SCSI address conflict with library {conflicts.first().library_id}")

        return JsonResponse({
            'success': True,
            'valid': len(validation_issues) == 0,
            'issues': validation_issues,
            'mhvtl_validation': config_valid
        })

    except Library.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Library not found'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@require_POST
def preview_config_ajax(request, library_id):
    """AJAX endpoint to preview library configuration (UPDATED to show actual MHVTL config)"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'})

    try:
        library = get_object_or_404(Library, library_id=library_id, is_active=True)

        # A library device.conf already declares is shown as it is written;
        # one only the database knows is shown as create() would write it.
        current = LibraryService().config_text(library_id)
        if current.success:
            preview, source, warnings = current.data['text'], 'device.conf', []
        else:
            planned = lifecycle.preview(_spec_for(library))
            if not planned.success:
                return JsonResponse({'success': False, 'error': planned.message,
                                     'errors': planned.errors})
            preview, source = planned.data['text'], 'planned'
            # The form shows one list; preview keeps errors and warnings apart.
            warnings = planned.data['errors'] + planned.data['warnings']

        return JsonResponse({
            'success': True,
            'preview': preview,
            'source': source,
            'warnings': warnings,
            'library_id': library_id,
            'format': 'mhvtl_device_conf'
        })

    except Library.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Library not found'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })

# =============================================================================
# Discovery Integration AJAX Functions
# =============================================================================

from apps.libraries.services.sync.service import sync_mhvtl_to_django


@require_POST
def refresh_discovery_ajax(request):
    """AJAX endpoint to refresh discovery (quick sync with device.conf)"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        stats = sync_mhvtl_to_django()

        return JsonResponse({
            'success': True,
            'message': f'Sync complete: {stats["libraries_found"]} libraries found, '
                       f'{stats["created"]} new, {stats["drives_imported"]} drives imported',
            'libraries': stats['total_db'],
            'drives': stats['total_drives'],
            'media': 0,
        })

    except Exception as e:
        logger.error(f"Error refreshing discovery: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@require_POST
def run_discovery_ajax(request):
    """AJAX endpoint to run full discovery scan (sync with device.conf)"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        stats = sync_mhvtl_to_django()

        return JsonResponse({
            'success': True,
            'message': f'Full scan complete: {stats["libraries_found"]} libraries, '
                       f'{stats["created"]} new, {stats["drives_imported"]} drives',
            'libraries': stats['total_db'],
            'drives': stats['total_drives'],
            'media': 0,
        })

    except Exception as e:
        logger.error(f"Error running full discovery: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@require_POST
def sync_library_ajax(request, library_id):
    """AJAX endpoint to sync a specific library with MHVTL device.conf"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        # Run full sync first to ensure this library exists in DB
        sync_mhvtl_to_django()

        library = get_object_or_404(Library, library_id=library_id, is_active=True)

        # Update sync timestamp
        library.last_synced = timezone.now()
        library.save()

        # Log the operation
        try:
            LibraryOperation.objects.create(
                library=library,
                operation='SYNC',
                description=f'Library {library_id} synced via AJAX',
                user_session=request.session.session_key or 'unknown'
            )
        except Exception as log_error:
            logger.warning(f"Failed to log sync operation: {log_error}")

        return JsonResponse({
            'success': True,
            'message': f'Library {library_id} synced successfully',
            'library_id': library_id,
            'last_synced': library.last_synced.isoformat()
        })

    except Library.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': f'Library {library_id} not found'
        })
    except Exception as e:
        logger.error(f"Error syncing library {library_id}: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@require_POST
def cleanup_orphaned_ajax(request):
    """AJAX endpoint to cleanup orphaned libraries not found in MHVTL config"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        # Parse request body
        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            data = {}

        dry_run = data.get('dry_run', True)

        # Current MHVTL library ids, from device.conf. An unreadable file must
        # not look like "no libraries", which would mark every one orphaned.
        listed = LibraryService().list(with_contents=False)
        if not listed.success:
            return JsonResponse({'success': False, 'error': listed.message})
        mhvtl_ids = {lib['library_id'] for lib in listed.data['libraries']}

        # Find orphaned DB libraries (active in Django but not in device.conf)
        orphaned = Library.objects.filter(is_active=True).exclude(library_id__in=mhvtl_ids)
        orphaned_ids = list(orphaned.values_list('library_id', flat=True))
        orphaned_drive_count = Drive.objects.filter(library__in=orphaned).count()

        deleted_libs = 0
        deleted_drives = 0

        if not dry_run and orphaned.exists():
            deleted_drives = Drive.objects.filter(library__in=orphaned).delete()[0]
            deleted_libs = orphaned.delete()[0]

        return JsonResponse({
            'success': True,
            'dry_run': dry_run,
            'discovered_count': len(mhvtl_ids),
            'orphaned_libraries': orphaned_ids,
            'orphaned_drives': orphaned_drive_count,
            'deleted_libraries': deleted_libs,
            'deleted_drives': deleted_drives,
            'errors': [],
            'message': f"{'Would delete' if dry_run else 'Deleted'} "
                       f"{len(orphaned_ids)} orphaned libraries and "
                       f"{orphaned_drive_count} drives"
        })

    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


def discovery_stats_ajax(request):
    """Get discovery statistics for dashboard display"""
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        active_libs = Library.objects.filter(is_active=True)
        active_drives = Drive.objects.filter(is_active=True)

        return JsonResponse({
            'success': True,
            'discovery_available': True,
            'latest_session': None,
            'current_status': {
                'status': 'available',
                'last_scan': None,
                'libraries_found': active_libs.count(),
                'drives_found': active_drives.count(),
                'media_found': 0,
            },
            'database_counts': {
                'libraries': active_libs.count(),
                'drives': active_drives.count(),
                'discovered_libraries': active_libs.filter(
                    discovery_status='discovered'
                ).count(),
                'created_libraries': active_libs.filter(
                    discovery_status='created'
                ).count(),
            }
        })

    except Exception as e:
        logger.error(f"Error getting discovery stats: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=500)


# =============================================================================
# Tape Operations with LTO Compatibility AJAX Endpoints
# =============================================================================

def get_library_status_with_lto_ajax(request, library_id):
    """
    AJAX endpoint to get library status with LTO generation information.

    Returns drive and tape LTO generations for compatibility checking.
    """
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        result = mounting.mount_status(library_id)
        status = ({'success': True, **result.data} if result.success
                  else {'success': False, 'error': result.message,
                        'library_id': library_id})

        return JsonResponse(status)

    except Exception as e:
        logger.error(f"Error getting library status with LTO: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e),
            'library_id': library_id
        })


def check_mount_compatibility_ajax(request, library_id):
    """
    AJAX endpoint to check tape/drive compatibility before mounting.

    Query parameters:
        slot: Source slot number
        drive: Target drive number

    Returns compatibility status with detailed explanation.
    """
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        slot = request.GET.get('slot')
        drive = request.GET.get('drive')

        if slot is None or drive is None:
            return JsonResponse({
                'success': False,
                'error': 'Missing required parameters: slot and drive'
            })

        try:
            slot = int(slot)
            drive = int(drive)
        except ValueError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid slot or drive number'
            })

        result = mounting.mount_check(library_id, slot, drive)

        return JsonResponse({
            'success': True,
            **result
        })

    except Exception as e:
        logger.error(f"Error checking mount compatibility: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


def get_drive_info_ajax(request, library_id):
    """
    AJAX endpoint to get drive information including LTO generations.

    Returns list of drives with their LTO generation and compatibility info.
    """
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Authentication required'}, status=401)

    try:
        drives = mounting.library_drives(library_id)

        return JsonResponse({
            'success': True,
            'library_id': library_id,
            'drives': drives,
            'drive_count': len(drives)
        })

    except Exception as e:
        logger.error(f"Error getting drive info: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e),
            'library_id': library_id
        })


def dashboard_summary_ajax(request):
    """Overview data for the dashboard tiles.

    Split out from the page render so a slow systemctl or lsmod call delays one
    tile instead of the whole page.
    """
    from apps.libraries.services.dashboard.service import get_dashboard_summary

    try:
        summary = get_dashboard_summary()
    except Exception as exc:  # noqa: BLE001 - the tiles show the message
        logger.exception('dashboard summary failed')
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)

    return JsonResponse({'success': True, 'summary': summary})
