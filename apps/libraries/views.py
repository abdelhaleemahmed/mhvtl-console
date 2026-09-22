# apps/libraries/views.py - library pages, on apps/libraries/services
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib import messages
from django.http import Http404, HttpResponse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import zipfile
from io import BytesIO

logger = logging.getLogger(__name__)

# Model imports
from .models import (
    Library, Drive, LibraryBrand, LibraryModel, 
    MediaSlot, LibraryOperation
)

# Every page here reads and changes libraries through apps/libraries/services.
# They used to go through adapters/mhvtl_library_service.py and
# tape_operations_service.py, behind an import guard that turned any import
# error into "service not available".
from apps.libraries.services.config.service import ConfigService
from apps.libraries.post_only import RedirectOnGet
from apps.libraries.services.drives import DriveService
from apps.libraries.services.tapes import TapeService
from apps.libraries.services.libraries import LibraryService, lifecycle, orphans
from apps.libraries.services.libraries import validation as library_validation
from apps.libraries.services.profiles import personalities
from apps.libraries.services.profiles.data import get_profile, get_profile_options, MEDIA_SUFFIX

# Kept for the templates and branches that still test them; the services are
# part of this app and always importable.
MHVTL_SERVICE_AVAILABLE = True
TAPE_SERVICE_AVAILABLE = True



@dataclass
class ConfiguredLibrary:
    """A device.conf library, in the shape the library pages read.

    status is "active" when its library_contents file can be read and
    "missing_contents" when it cannot.
    """
    library_id: int
    vendor: str
    product: str
    serial: str
    channel: int
    target: int
    lun: int
    status: str
    naa: str = ''
    home_directory: str = ''
    drives: int = 0
    config_source: str = 'device.conf'


def _configured_libraries():
    """Every library device.conf declares. Raises when it cannot be read, so a
    page can say so rather than show an empty host."""
    listed = LibraryService().list(with_contents=True)
    if not listed.success:
        raise RuntimeError(listed.message)
    return [ConfiguredLibrary(
        library_id=lib['library_id'], vendor=lib['vendor'] or 'Unknown',
        product=lib['product'] or 'Unknown', serial=lib['serial'] or 'Unknown',
        channel=lib['channel'] or 0, target=lib['target'] or 0, lun=lib['lun'] or 0,
        status='active' if lib['slot_count'] is not None else 'missing_contents',
        naa=lib['naa'], home_directory=lib['home_directory'], drives=lib['drives'])
        for lib in listed.data['libraries']]


def _configured_drives(library_id):
    """A library's drives from device.conf, with the fields the detail page reads."""
    conf = ConfigService().device_conf()
    if conf is None:
        return []
    return [SimpleNamespace(drive_id=drive_id, library_id=library_id,
                            slot=data.get('slot'), channel=data.get('channel'),
                            target=data.get('target'), lun=data.get('lun'),
                            vendor=data.get('vendor'), product=data.get('product'),
                            serial=data.get('serial'), naa=data.get('naa'))
            for drive_id, data in sorted(conf.drives_of(int(library_id)).items())]


def _next_library_id():
    """The next free library id; ValueError when there is none."""
    result = LibraryService().next_id()
    if not result.success:
        raise ValueError('; '.join(result.errors) or result.message)
    return result.data['library_id']


def _mhvtl_service_status():
    """Whether MHVTL is running.

    This used to call MHVTLLibraryService.get_service_status(), a method that
    does not exist. The AttributeError was caught and turned into
    {'available': False}, so this page reported MHVTL as unavailable on a
    perfectly healthy host. services/console knows.
    """
    try:
        from apps.libraries.services.console import units
        status = units.status()
        return {
            'available': True,
            'running': status.target_active,
            'enabled': status.target_enabled,
            'healthy': status.healthy,
            'libraries_active': status.libraries_active,
            'drives_active': status.drives_active,
        }
    except Exception as exc:  # noqa: BLE001 - shown to the operator
        logger.warning('reading MHVTL service status: %s', exc)
        return {'available': False, 'error': str(exc)}


def _monitor_metrics(library_id: int) -> dict:
    """One library's metrics, in the shape the monitor page and its partials read.

    From services/console (metrics.for_library, units); the shaping used to
    be adapters/monitoring_service.py. The drive count comes from device.conf,
    and a stopped daemon's uptime is None, not "Running".
    """
    from apps.libraries.services.console import metrics, units

    collected = metrics.for_library(library_id)
    process = collected.process
    usage = collected.disk_usage
    pids = units.pids_of(collected.unit) if collected.running else []
    return {
        'library_id': collected.library_id,
        'is_running': collected.running,
        'service_status': {
            'name': collected.unit,
            'is_running': collected.running,
            'is_enabled': units.is_enabled(collected.unit),
            'pid': pids[0] if pids else None,
            'uptime': collected.uptime,
            'error': None,
        },
        'metrics': {
            'cpu': process.cpu_percent if process else None,
            'memory': process.memory_percent if process else None,
            'memory_mb': process.memory_mb if process else None,
            'disk': usage.percent if usage else None,
            'disk_used_gb': usage.used_gb if usage else None,
            'disk_total_gb': usage.size_gb if usage else None,
            'uptime': collected.uptime if collected.running else 'Stopped',
        },
        'drive_status': {'online': collected.drives_online,
                         'total': collected.drives_total},
        'error_count': 0,
    }



class LibraryDashboardView(View):
    """Main dashboard view showing library overview with real statistics"""
    template_name = 'libraries/dashboard.html'

    def _sync_django_with_device_conf(self):
        """
        Sync Django DB with device.conf via the shared sync service.
        Returns (synced_count, error_message)
        """
        if not MHVTL_SERVICE_AVAILABLE:
            return 0, None

        try:
            from apps.libraries.services.sync.service import sync_mhvtl_to_django
            stats = sync_mhvtl_to_django()
            synced = stats['created'] + stats['activated'] + stats['deactivated']
            return synced, None
        except Exception as e:
            return 0, str(e)

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # Auto-sync Django DB with device.conf
        synced, sync_error = self._sync_django_with_device_conf()
        if sync_error:
            messages.warning(request, f"Sync warning: {sync_error}")

        # Get libraries from MHVTL service (authoritative source for vendor/product)
        # This ensures we always display correct data from device.conf
        libraries = []
        total_drives = 0

        if MHVTL_SERVICE_AVAILABLE:
            try:
                mhvtl_libraries = _configured_libraries()

                # Build library data with correct vendor/product from device.conf
                for mhvtl_lib in mhvtl_libraries:
                    # Get Django DB record for additional metadata (if exists)
                    db_lib = Library.objects.filter(library_id=mhvtl_lib.library_id).first()

                    libraries.append({
                        'library_id': mhvtl_lib.library_id,
                        'vendor': mhvtl_lib.vendor,  # From device.conf (authoritative)
                        'product': mhvtl_lib.product,  # From device.conf (authoritative)
                        'serial': mhvtl_lib.serial,
                        'status': mhvtl_lib.status,
                        'drives_count': mhvtl_lib.drives,
                        'total_slots': db_lib.total_slots if db_lib else 0,
                        'media_count': db_lib.media_count if db_lib else 0,
                        'empty_slots': db_lib.empty_slots if db_lib else 0,
                        'discovery_status': db_lib.discovery_status if db_lib else 'discovered',
                        'mhvtl_id': db_lib.mhvtl_id if db_lib else None,
                        'last_synced': db_lib.last_synced if db_lib else None,
                    })
                    total_drives += mhvtl_lib.drives

            except Exception as e:
                messages.warning(request, f"Could not read MHVTL config: {str(e)}")
                # Fall back to Django DB if MHVTL service fails
                db_libraries = Library.objects.filter(is_active=True).select_related('brand', 'model').order_by('library_id')
                for db_lib in db_libraries:
                    libraries.append({
                        'library_id': db_lib.library_id,
                        'vendor': db_lib.brand.name if db_lib.brand else db_lib.vendor_identification,
                        'product': db_lib.model.name if db_lib.model else db_lib.product_identification,
                        'serial': db_lib.unit_serial_number,
                        'status': 'active' if db_lib.is_active else 'inactive',
                        'drives_count': db_lib.drives.filter(is_active=True).count(),
                        'total_slots': db_lib.total_slots,
                        'media_count': db_lib.media_count,
                        'empty_slots': db_lib.empty_slots,
                        'discovery_status': db_lib.discovery_status,
                        'mhvtl_id': db_lib.mhvtl_id,
                        'last_synced': db_lib.last_synced,
                    })
                total_drives = Drive.objects.filter(is_active=True).count()
        else:
            # MHVTL service not available - use Django DB
            db_libraries = Library.objects.filter(is_active=True).select_related('brand', 'model').order_by('library_id')
            for db_lib in db_libraries:
                libraries.append({
                    'library_id': db_lib.library_id,
                    'vendor': db_lib.brand.name if db_lib.brand else db_lib.vendor_identification,
                    'product': db_lib.model.name if db_lib.model else db_lib.product_identification,
                    'serial': db_lib.unit_serial_number,
                    'status': 'active' if db_lib.is_active else 'inactive',
                    'drives_count': db_lib.drives.filter(is_active=True).count(),
                    'total_slots': db_lib.total_slots,
                    'media_count': db_lib.media_count,
                    'empty_slots': db_lib.empty_slots,
                    'discovery_status': db_lib.discovery_status,
                    'mhvtl_id': db_lib.mhvtl_id,
                    'last_synced': db_lib.last_synced,
                })
            total_drives = Drive.objects.filter(is_active=True).count()

        # Get recent operations (last 10)
        try:
            recent_operations = LibraryOperation.objects.select_related('library').order_by('-created_at')[:10]
        except:
            recent_operations = []

        # Count operations by type (last 24 hours)
        from django.utils import timezone
        from datetime import timedelta
        day_ago = timezone.now() - timedelta(days=1)
        try:
            recent_create = LibraryOperation.objects.filter(
                operation='CREATE',
                created_at__gte=day_ago
            ).count()
            recent_update = LibraryOperation.objects.filter(
                operation='UPDATE',
                created_at__gte=day_ago
            ).count()
            recent_delete = LibraryOperation.objects.filter(
                operation='DELETE',
                created_at__gte=day_ago
            ).count()
        except:
            recent_create = recent_update = recent_delete = 0

        # MHVTL service status. This called MHVTLLibraryService.get_service_status(),
        # which does not exist, and a bare except turned that into "Unable to
        # check service status" on every load.
        status = _mhvtl_service_status()
        if not status['available']:
            mhvtl_status = 'error'
            mhvtl_message = f"Unable to check service status: {status['error']}"
        elif status['running']:
            mhvtl_status = 'running'
            mhvtl_message = 'MHVTL service is running'
        else:
            mhvtl_status = 'stopped'
            mhvtl_message = 'MHVTL service is not running'

        # Basic context with enhanced statistics
        context = {
            'libraries': libraries,
            'library_count': len(libraries),
            'total_drives': total_drives,
            'recent_operations': recent_operations,
            'operations_count': recent_operations.count() if recent_operations else 0,
            'recent_create_count': recent_create,
            'recent_update_count': recent_update,
            'recent_delete_count': recent_delete,
            'mhvtl_status': mhvtl_status,
            'mhvtl_message': mhvtl_message,
            'title': 'Library Management Dashboard'
        }

        # There is no discovery app - these came from one that was never
        # built, and total_media counted MediaSlot rows, a table nothing
        # writes, so the tile always read 0. The tapes are in
        # library_contents; the libraries are what device.conf declares.
        context.update({
            'discovered_libraries': len(libraries),
            'created_libraries': len(libraries),
            'total_media': _tape_count(),
        })

        return render(request, self.template_name, context)


class SetupChoiceView(View):
    """Setup workflow - choose between standard and custom setup"""
    template_name = 'libraries/setup_choice.html'
    
    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        
        context = {
            'title': 'Choose Setup Method'
        }
        return render(request, self.template_name, context)
    
    def post(self, request):
        """Handle setup choice form submission"""
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        
        setup_type = request.POST.get('setup_type')
        
        if setup_type == 'standard':
            # Redirect to brand selection for standard setup
            return redirect('libraries:brand_selection')
        elif setup_type == 'custom':
            # Redirect to custom setup
            return redirect('libraries:custom_setup')
        else:
            messages.error(request, 'Please select a setup method.')
            return self.get(request)


def _brand_catalogue():
    """Every vendor the application can create, from the profiles.

    The profiles are the authority: they are what validation, the create form
    and MHVTL itself agree on. This page used to list LibraryBrand rows, and
    that table had grown case-duplicated pairs (Dell and DELL, Spectra and
    SPECTRA, ...) and a TestVendor left by a test run, while a profile with no
    row would not have appeared at all.
    """
    from apps.libraries.services.profiles import PROFILES, personalities
    from apps.libraries.services.profiles.data import get_valid_drives_for_library

    catalogue = []
    for key, profile in sorted(PROFILES.items()):
        media, drives = set(), set()
        for model in profile.library_models:
            for drive in get_valid_drives_for_library(key, model):
                drives.add(drive)
                media.update(profile.drive_media_by_model.get(drive, ()))
        catalogue.append({
            'key': key,
            'display': profile.library_vendor,
            'models': list(profile.library_models),
            'model_count': len(profile.library_models),
            'drive_count': len(drives),
            'media': sorted(media, key=lambda m: (_media_family(m), m)),
            'families': sorted({_media_family(m) for m in media}),
            'newest_lto': _newest_lto(media),
            'default_model': profile.library_product_default,
            'layouts': sorted({personalities.library_layout(profile.library_vendor,
                                                            model).title
                               for model in profile.library_models}),
        })
    return catalogue


def _media_family(density: str) -> str:
    """LTO, T10000, 9840, 9940, 3592, AIT, SDLT or DLT, for grouping."""
    for prefix, family in (('LTO', 'LTO'), ('T10K', 'T10000'), ('9840', '9840'),
                           ('9940', '9940'), ('AIT', 'AIT'), ('SDLT', 'SDLT'),
                           ('DLT', 'DLT')):
        if density.startswith(prefix):
            return family
    return {'J1A': '3592', 'E05': '3592', 'E06': '3592', 'E07': '3592'}.get(
        density, density)


def _newest_lto(media):
    """'LTO-10' for the newest LTO generation in `media`, or None."""
    from apps.libraries.services.profiles.data import lto_generation

    generations = [int(m[3:]) for m in media
                   if m.startswith('LTO') and m[3:].isdigit()]
    if 'LTO10' in media or 'LTO10P' in media:
        generations.append(10)
    return f'LTO-{max(generations)}' if generations else None


class BrandSelectionView(View):
    """Setup workflow - choose the vendor, or the media and then the vendor."""
    template_name = 'libraries/brand_selection.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        from apps.libraries.services.profiles import personalities

        brands = _brand_catalogue()
        offered = sorted({m for brand in brands for m in brand['media']},
                         key=lambda m: (_media_family(m), m))
        context = {
            'brands': brands,
            # "which vendors take this tape?", the filter on the page
            'media_choices': [{'density': m, 'label': personalities.media_label(m),
                               'family': _media_family(m)} for m in offered],
            'selected_media': request.GET.get('media', ''),
            'title': 'Select Library Brand',
        }
        return render(request, self.template_name, context)

    def post(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        brand_name = request.POST.get('brand')
        if brand_name:
            return redirect('libraries:brand_config', brand_name=brand_name)
        messages.error(request, 'Please select a brand.')
        return self.get(request)


def _wanted_media(request, profile_options):
    """The tape the operator asked for, when this vendor can take it.

    Comes from ?media= - the vendor page's filter links carry it - and is
    ignored rather than refused when it is not one this vendor's drives load,
    so a stale link opens the page instead of failing.
    """
    wanted = (request.GET.get('media') or '').upper()
    return wanted if wanted in profile_options['media_types'] else ''


def _tape_count():
    """How many tapes the configured libraries hold, from library_contents."""
    from apps.libraries.services.config.service import ConfigService
    from apps.libraries.services.libraries import LibraryService

    listed = LibraryService().list(with_contents=False)
    if not listed.success:
        return 0
    config = ConfigService()
    total = 0
    for library in listed.data['libraries']:
        contents = config.library_contents(library['library_id'])
        total += len(contents.occupied) if contents is not None else 0
    return total


def _sync_database(request):
    """Bring the database back in line with device.conf, and say so."""
    from apps.libraries.services.sync.service import sync_mhvtl_to_django

    try:
        stats = sync_mhvtl_to_django()
    except Exception as exc:  # noqa: BLE001 - shown to the operator
        logger.warning('syncing the database with device.conf: %s', exc)
        messages.warning(request, f'The database was not updated: {exc}')
        return None
    messages.info(request, 'Database updated from device.conf')
    return stats


def _free_targets():
    """SCSI targets left on this host, or None when device.conf is unreadable.

    Each drive of a new library takes one, as does the library, so this -
    not the model's element layout - is usually what limits the drive count.
    """
    from apps.libraries.services.config import ids
    from apps.libraries.services.config.service import ConfigService

    conf = ConfigService().device_conf()
    return ids.free_targets(conf) if conf is not None else None


class BrandConfigView(View):
    """Setup workflow - configure specific brand using the library service"""
    template_name = 'libraries/brand_config.html'
    
    def get(self, request, brand_name):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # The profile is the authority; the database row only carries a
        # display name, and a profile may have no row at all.
        from apps.libraries.services.profiles import PROFILES

        if brand_name.upper() not in PROFILES:
            raise Http404(f'No vendor profile named {brand_name!r}')
        brand = (LibraryBrand.objects.filter(name__iexact=brand_name,
                                             is_active=True).first()
                 or SimpleNamespace(name=brand_name.upper(),
                                    display_name=brand_name.upper(), pk=None))
        models = (LibraryModel.objects.filter(brand=brand, is_active=True)
                  if brand.pk else [])

        # Get next available library ID using the actual service method
        # Default to 10 (first standard MHVTL library ID) if service unavailable
        next_id = 10
        if MHVTL_SERVICE_AVAILABLE:
            try:
                next_id = _next_library_id()
            except Exception as e:
                messages.warning(request, f"Could not determine next library ID: {str(e)}")

        # Get profile for barcode prefix and cascading dropdown data
        default_barcode_prefix = "L" + f"{next_id:02d}"  # Fallback
        profile_key = brand.name.upper()
        profile_json = '{}'  # Fallback empty JSON
        default_media_count = default_empty_slots = 0
        try:
            profile = get_profile(profile_key)
            default_barcode_prefix = profile.barcode_leading + f"{next_id:02d}"
            # Get full profile options for cascading dropdowns
            profile_options = get_profile_options(profile_key)
            profile_options['media_suffixes'] = MEDIA_SUFFIX
            profile_options['free_targets'] = _free_targets()
            # "I want an LTO-10 library": the tape carries over from the vendor
            # page's filter, and can be changed on this page. Choosing one
            # leaves only the models and drives that take it.
            profile_options['media_labels'] = {
                density: personalities.media_label(density)
                for density in profile_options['media_types']}
            profile_options['wanted_media'] = _wanted_media(request, profile_options)
            profile_json = json.dumps(profile_options)
            # The form's starting counts come from the profile, so the command
            # line, the service and this page cannot disagree about them.
            default_media_count = profile_options['defaults']['media_count']
            default_empty_slots = profile_options['defaults']['empty_slots']
        except Exception as e:
            messages.warning(request, f"Could not load profile data: {str(e)}")

        # SCSI addressing - Channel is always 0, Target/LUN are auto-calculated during creation
        next_channel = 0
        next_target = 0  # Will be auto-calculated by service based on available targets
        next_lun = 0     # Always 0 for MHVTL

        context = {
            'brand': brand,
            'models': models,
            'profile_key': profile_key,
            'profile_json': profile_json,
            'next_library_id': next_id,
            'next_channel': next_channel,
            'next_target': next_target,
            'next_lun': next_lun,
            'default_barcode_prefix': default_barcode_prefix,
            'default_media_count': default_media_count,
            'default_empty_slots': default_empty_slots,
            'title': f'Configure {brand.display_name} Library'
        }
        return render(request, self.template_name, context)
    
    def post(self, request, brand_name):
        """Handle brand configuration form submission using the library service"""
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        if not MHVTL_SERVICE_AVAILABLE:
            messages.error(request, "❌ MHVTL service not available - cannot create library")
            return redirect('libraries:brand_config', brand_name=brand_name)

        try:
            brand = get_object_or_404(LibraryBrand, name=brand_name, is_active=True)
            profile_key = brand.name.upper()

            # Get next available library ID using the actual service method
            library_id = _next_library_id()

            # Get form data - now using profile-based library_model instead of database model_id
            library_model = request.POST.get('library_model', '')
            drive_model = request.POST.get('drive_model', '')
            media_type = request.POST.get('media_type', '')
            num_drives = int(request.POST.get('num_drives', 4))
            media_count = int(request.POST.get('media_count', 0))
            barcode_prefix = request.POST.get('barcode_prefix', 'E01').upper()
            empty_slots = int(request.POST.get('empty_slots', 0))
            map_count = request.POST.get('map_count', '')

            # Validate required fields
            if not library_model:
                messages.error(request, "❌ Please select a library model")
                return redirect('libraries:brand_config', brand_name=brand_name)
            if not drive_model:
                messages.error(request, "❌ Please select a drive model")
                return redirect('libraries:brand_config', brand_name=brand_name)
            if not media_type:
                messages.error(request, "❌ Please select a media type")
                return redirect('libraries:brand_config', brand_name=brand_name)

            # Prepare library data exactly as the service expects
            # Now using library_model directly from profile instead of database
            library_data = {
                'library_id': library_id,
                'profile': profile_key,  # Profile key for vendor lookup
                'library_model': library_model,  # For cascading validation
                'product': library_model,  # Product identification (same as library_model)
                'serial': request.POST.get('unit_serial_number', f'XYZZY_{library_id}'),
                'num_drives': num_drives,
                'drive_model': drive_model,  # For cascading validation
                'drive_product': drive_model,  # Service expects drive_product
                # Media configuration for library_contents generation
                'media_type': media_type,
                'media_count': media_count,
                'barcode_prefix': barcode_prefix,
                'empty_slots': empty_slots,
            }
            # The form used to send num_maps: 4, a key nothing reads; with no
            # map_count the spec fits the profile default to the model.
            if map_count.strip():
                library_data['map_count'] = int(map_count)

            # The create-restart-verify-media sequence lives in
            # services.libraries.workflow. It used to be written out here,
            # interleaved with messages.* calls, which is why there was no way to
            # create a library from anywhere but this form.
            from apps.libraries.services.libraries import create_library_workflow

            result = create_library_workflow(library_data)

            for step in (result.data or {}).get('steps', []):
                if step['ok']:
                    messages.success(request, step['message'])
                elif step['fatal']:
                    messages.error(request, step['message'])
                else:
                    messages.warning(request, step['message'])

            if not result.success:
                for error in result.errors:
                    messages.error(request, f'• {error}')
                return redirect('libraries:brand_config', brand_name=brand_name)

            # The database is a cache of what device.conf says, so it is
            # filled from device.conf rather than from the form: this used to
            # build the row by hand from keys the form does not send (it failed
            # with KeyError 'vendor' on every creation), with channel, target
            # and LUN hardcoded to 0, a made-up NAA, no brand or model, and
            # drive ids guessed as library_id + n.
            try:
                from apps.libraries.services.sync.service import sync_mhvtl_to_django
                sync_mhvtl_to_django()
                messages.success(request,
                                 f'Library {library_id} recorded in the database')
            except Exception as db_error:  # noqa: BLE001 - shown to the operator
                logger.warning('recording library %s in the database: %s',
                               library_id, db_error)
                messages.warning(
                    request,
                    f'Library created in MHVTL but the database record failed: '
                    f'{db_error}')

            return redirect('libraries:dashboard')

        except Exception as e:
            messages.error(request, f'❌ Unexpected error creating library: {str(e)}')
            return redirect('libraries:brand_config', brand_name=brand_name)


class CustomSetupView(View):
    """Setup workflow - custom configuration"""
    template_name = 'libraries/custom_setup.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        brands = LibraryBrand.objects.filter(is_active=True)

        context = {
            'brands': brands,
            'title': 'Custom Library Setup'
        }
        return render(request, self.template_name, context)

    def post(self, request):
        """Handle custom library creation"""
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        if not MHVTL_SERVICE_AVAILABLE:
            messages.error(request, "❌ MHVTL service not available")
            return redirect('libraries:custom_setup')

        try:
            service = LibraryService()

            # Get next available library ID
            library_id = _next_library_id()

            # Prepare library data from custom form
            library_data = {
                'library_id': library_id,
                'vendor': request.POST.get('vendor', '').upper(),
                'product': request.POST.get('product', ''),
                'serial': request.POST.get('serial', f'XYZZY_{library_id}'),
                'num_drives': int(request.POST.get('num_drives', 4)),
            }

            # Optional SCSI parameters
            if request.POST.get('channel'):
                library_data['channel'] = int(request.POST.get('channel'))
            if request.POST.get('target'):
                library_data['target'] = int(request.POST.get('target'))
            if request.POST.get('lun'):
                library_data['lun'] = int(request.POST.get('lun'))

            # Validate
            validation = library_validation.validate(library_data)
            if not validation.is_valid:
                for error in validation.errors:
                    messages.error(request, f"❌ {error}")
                return redirect('libraries:custom_setup')

            # Create library
            result = service.create(library_data)

            if result.success:
                messages.success(request, f"✅ {result.message}")

                # Restart services
                try:
                    restart_result = service.restart_services(library_id)
                    if restart_result.success:
                        messages.success(request, "🔄 Services restarted")
                except Exception as e:
                    messages.warning(request, f"⚠️ Service restart failed: {str(e)}")

                # Verify library is recognized by MHVTL
                try:
                    if service.recognised_by_mhvtl(library_id):
                        messages.success(request, f"✓ Library {library_id} verified in MHVTL system")
                    else:
                        messages.warning(request, f"⚠️ Library {library_id} created but not yet recognized by MHVTL")
                except Exception as verify_error:
                    messages.warning(request, f"⚠️ Could not verify library status: {str(verify_error)}")

                # Create Django DB record
                try:
                    brand = LibraryBrand.objects.filter(name=library_data['vendor']).first()
                    if brand:
                        Library.objects.create(
                            library_id=library_id,
                            brand=brand,
                            vendor_identification=library_data['vendor'],
                            product_identification=library_data['product'],
                            unit_serial_number=library_data['serial'],
                            channel=library_data.get('channel', 0),
                            target=library_data.get('target', 0),
                            lun=library_data.get('lun', 0),
                            is_active=True
                        )
                except Exception as e:
                    messages.warning(request, f"⚠️ Database save failed: {str(e)}")

                return redirect('libraries:dashboard')
            else:
                for error in result.errors:
                    messages.error(request, f"❌ {error}")
                return redirect('libraries:custom_setup')

        except Exception as e:
            messages.error(request, f"❌ Error: {str(e)}")
            return redirect('libraries:custom_setup')


class LibraryListView(View):
    """List all libraries from MHVTL config (authoritative source)"""
    template_name = 'libraries/library_list.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # Get libraries from actual MHVTL config (device.conf) - authoritative source
        mhvtl_libraries = []
        if MHVTL_SERVICE_AVAILABLE:
            try:
                mhvtl_libraries = _configured_libraries()
            except Exception as e:
                messages.warning(request, f"Could not read MHVTL config: {str(e)}")

        # Also get Django DB libraries for additional info
        db_libraries = Library.objects.all().select_related('brand', 'model')

        # Build combined list with MHVTL config as authoritative
        libraries = []
        for lib in mhvtl_libraries:
            db_lib = db_libraries.filter(library_id=lib.library_id).first()
            libraries.append({
                'library_id': lib.library_id,
                'vendor': lib.vendor,
                'product': lib.product,
                'serial': lib.serial,
                'channel': lib.channel,
                'target': lib.target,
                'lun': lib.lun,
                'status': lib.status,
                'naa': lib.naa,
                'home_directory': lib.home_directory,
                # Django DB info
                'in_database': db_lib is not None,
                'db_active': db_lib.is_active if db_lib else False,
                'brand': db_lib.brand if db_lib else None,
                'model': db_lib.model if db_lib else None,
            })

        context = {
            'libraries': libraries,
            'title': 'Library List'
        }
        return render(request, self.template_name, context)


def _library_row(library_id: int) -> Library:
    """The database row for a library, syncing once if it is not there yet.

    A library created from the command line exists in device.conf before the
    database has heard of it, and the detail page used to answer "No Library
    matches the given query" until some other page happened to run a sync -
    while the dashboard, reading the files, showed the library perfectly well.
    """
    try:
        return Library.objects.get(library_id=library_id, is_active=True)
    except Library.DoesNotExist:
        pass

    try:
        from apps.libraries.services.sync.service import sync_mhvtl_to_django
        sync_mhvtl_to_django()
    except Exception:                                  # noqa: BLE001 - falls through to 404
        logger.warning('sync while opening library %s', library_id, exc_info=True)
    return get_object_or_404(Library, library_id=library_id, is_active=True)


def _drive_activity(library_id: int) -> list:
    """What each drive is doing, for the first render of a page that polls.

    settle=0: one reading, no wait. A page that refreshes every few seconds
    brings its own comparison, and blocking the request for a second to say
    "writing" instead of "holding" is a bad trade on page load.

    A library whose daemons are stopped answers nothing, which is a row per
    drive saying so - not an error, and never a reason to fail the page.
    """
    try:
        result = DriveService().activity(library_id, settle=0)
    except Exception:                       # noqa: BLE001 - the page stands
        logger.warning('drive activity for library %s', library_id, exc_info=True)
        return []
    return (result.data or {}).get('drives', []) if result.success else []


class LibraryDetailView(View):
    """View details of a specific library"""
    template_name = 'libraries/library_detail.html'

    def get(self, request, library_id):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        library = _library_row(library_id)

        # Get drives from MHVTL config (authoritative source)
        drives_count = 0
        mhvtl_library_info = None
        mhvtl_drives = []
        if MHVTL_SERVICE_AVAILABLE:
            try:
                for lib_info in _configured_libraries():
                    if lib_info.library_id == library_id:
                        drives_count = lib_info.drives
                        mhvtl_library_info = lib_info
                        break
                mhvtl_drives = _configured_drives(library_id)
            except Exception:
                pass

        # Fallback to Django DB if MHVTL not available
        db_drives = library.drives.filter(is_active=True)
        if drives_count == 0:
            drives_count = db_drives.count()

        # Get live slot stats from library_contents file
        # library_contents is the truth about slots; the database row is the
        # fallback for when it cannot be read. The template used to choose
        # between them with |default:, which treats a real 0 as "missing" -
        # so a full library reported the stored empty-slot count instead of 0.
        slot_stats = {'total': library.total_slots,
                      'occupied': library.media_count,
                      'free': library.empty_slots, 'next_slot': None}
        # The tapes themselves: the page showed the drives and never said what
        # was in the slots, so the barcodes could only be found on the
        # operator's library-status page. They come from TapeService rather
        # than straight from library_contents, because the tiles show how full
        # each one is and that is the service's to work out.
        tapes = []
        if TAPE_SERVICE_AVAILABLE:
            try:
                contents = ConfigService().library_contents(library_id)
                if contents is not None:
                    slot_stats = contents.slot_stats()
            except Exception:
                pass
            try:
                listed = TapeService().list(library_id)
                if listed.success:
                    tapes = sorted(listed.data['tapes'],
                                   key=lambda t: t['slot'] or 0)
            except Exception:                  # noqa: BLE001 - the page stands
                logger.warning('tapes for library %s', library_id, exc_info=True)

        context = {
            'library': library,
            'drives': mhvtl_drives if mhvtl_drives else db_drives,
            'drives_count': drives_count,
            'mhvtl_info': mhvtl_library_info,
            'slot_stats': slot_stats,
            'tapes': tapes,
            # What each drive is doing, so the panel is right on arrival
            # rather than empty until the first refresh five seconds later.
            'activity_drives': _drive_activity(library_id),
            'title': f'Library {library_id} Details'
        }
        return render(request, self.template_name, context)


class LibraryConfigureView(View):
    """Disabled in this release.

    The page was the clearest summary of one library in the GUI, but its Save
    button posted vendor, product and serial to LibraryService.update(), which
    **deletes and recreates the library** - MHVTL reads those strings once,
    when the daemon starts, so there is no in-place edit - and the page said
    nothing about it. An innocent-looking form that rebuilds a library is not
    something to ship, so it answers with a note and sends the operator to the
    pages that do the same work safely. What replaces it is in the docs
    (guides/phase-two): a rebuild spelled out and confirmed, or a read-only
    page.
    """

    NOTE = ('Library configuration is disabled in this release: its Save '
            'rebuilt the library - stopping it, removing it from device.conf '
            'and creating it again - without saying so. Everything it could '
            'change is on this page, the operator panel and the tape '
            'inventory.')

    def get(self, request, library_id):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        messages.info(request, self.NOTE)
        return redirect('libraries:library_detail', library_id=library_id)

    def post(self, request, library_id):
        """Nothing is written: the form is gone, and a POST here is a stale
        page or a script that has not noticed."""
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        messages.warning(request, f'Nothing was changed. {self.NOTE}')
        return redirect('libraries:library_detail', library_id=library_id)


class LibraryControlView(RedirectOnGet, View):
    """Stop, start or restart one library, and change its empty slots.

    The detail page has posted to /libraries/control/<id>/ since it was
    written; the URL was commented out and this view did not exist, so its
    Stop button answered 404. The slot change is here too because it needs the
    same restart to reach the robot: library_contents is read once, when the
    daemon starts.
    """

    # The library page's form: a GET goes back to that page.
    page = 'libraries:library_detail'

    def post(self, request, library_id):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        action = request.POST.get('action', '')
        back = redirect('libraries:library_detail', library_id=library_id)

        if action == 'slots':
            return self._slots(request, library_id, back)
        if action in ('stop', 'start', 'restart'):
            return self._units(request, library_id, action, back)

        messages.error(request, f'Unknown action {action!r}')
        return back

    def _drive_ids(self, library_id: int):
        conf = ConfigService().device_conf()
        return sorted(conf.drives_of(int(library_id))) if conf else []

    def _units(self, request, library_id, action, back):
        from apps.libraries.services.console import units

        drive_ids = self._drive_ids(library_id)
        if action == 'stop':
            outcome = units.stop_library(library_id, drive_ids)
        elif action == 'start':
            outcome = units.start_library(library_id, drive_ids)
        else:
            restarted = units.restart_library(library_id)
            outcome = {restarted['restarted']: restarted['ok']}

        done = {'stop': 'stopped', 'start': 'started', 'restart': 'restarted'}[action]
        failed = [unit for unit, ok in outcome.items() if not ok]
        if failed:
            messages.warning(
                request,
                f'{action.title()} finished with problems: '
                f'{", ".join(failed)} did not {action}')
        else:
            messages.success(request,
                             f'Library {library_id}: {len(outcome)} unit(s) {done}')
        return back

    def _slots(self, request, library_id, back):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import lifecycle

        try:
            empty = int(request.POST.get('empty_slots', ''))
        except ValueError:
            messages.error(request, 'Give a number of empty slots')
            return back

        result = lifecycle.set_empty_slots(library_id, empty)
        if not result.success:
            messages.error(request, result.message)
            for problem in result.errors:
                messages.warning(request, problem)
            return back

        messages.success(request, result.message)
        if request.POST.get('restart') == 'on':
            outcome = units.restart_library(library_id)
            if outcome.get('ok'):
                messages.success(request, f'Restarted {outcome["restarted"]}')
            else:
                messages.warning(
                    request,
                    f'The slots were written, but {outcome["restarted"]} did not '
                    f'restart: {outcome.get("error") or "systemctl reported a failure"}')
        else:
            messages.info(request,
                          'Restart the library for its robot to see the new slots')
        return back


class LibraryMonitorView(View):
    """Monitor one library: its daemon, its cost, its disk and its drives."""
    template_name = 'libraries/library_monitor.html'

    def get(self, request, library_id):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # Library can be active or inactive - we allow monitoring stopped libraries
        library = get_object_or_404(Library, library_id=library_id)
        drives = library.drives.filter(is_active=True)

        metrics = None
        try:
            metrics = _monitor_metrics(library_id)
        except Exception as e:  # noqa: BLE001 - shown to the operator
            messages.warning(request, f"Could not get metrics: {str(e)}")

        context = {
            'library': library,
            'drives': drives,
            'metrics': metrics,
            'total_slots': library.total_slots,
            # The page is called Monitor and said nothing about what the
            # drives were doing; this is the same panel the library page has.
            'activity_drives': _drive_activity(library_id),
            'title': f'Monitor Library {library_id}',
            'breadcrumb_items': [
                {'title': 'Libraries', 'url': 'libraries:dashboard'},
                {'title': f'Library {library_id}'},
            ]
        }
        return render(request, self.template_name, context)


class LibraryMonitorMetricsView(View):
    """HTMX partial view for refreshing monitor metrics"""

    def get(self, request, library_id):
        if not request.session.get('mhvtl_logged_in'):
            return HttpResponse('Unauthorized', status=401)

        library = get_object_or_404(Library, library_id=library_id)

        metrics = None
        try:
            metrics = _monitor_metrics(library_id)
        except Exception:  # noqa: BLE001 - the partial shows "no metrics"
            logger.exception('monitor metrics for library %s', library_id)

        # Determine which partial to render based on request
        partial = request.GET.get('partial', 'status')

        if partial == 'system':
            template = 'libraries/partials/_monitor_system_metrics.html'
        else:
            template = 'libraries/partials/_monitor_metrics.html'

        context = {
            'metrics': metrics,
            'total_slots': library.total_slots,
        }
        return render(request, template, context)


class LibraryRemoveView(View):
    """Remove library workflow using actual service"""
    template_name = 'libraries/library_remove.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # Get libraries from actual MHVTL config (device.conf) - this is the authoritative source
        mhvtl_libraries = []
        if MHVTL_SERVICE_AVAILABLE:
            try:
                mhvtl_libraries = _configured_libraries()
            except Exception as e:
                messages.warning(request, f"Could not read MHVTL config: {str(e)}")

        # Also get Django DB libraries for reference (may include inactive ones)
        db_libraries = Library.objects.all()

        # Build combined list with MHVTL config as authoritative
        libraries = []
        for lib in mhvtl_libraries:
            # Check if in Django DB
            db_lib = db_libraries.filter(library_id=lib.library_id).first()
            libraries.append({
                'library_id': lib.library_id,
                'vendor': lib.vendor,
                'product': lib.product,
                'serial': lib.serial,
                'status': lib.status,
                'in_database': db_lib is not None,
                'db_active': db_lib.is_active if db_lib else False,
            })

        context = {
            'libraries': libraries,
            'title': 'Remove Library'
        }
        return render(request, self.template_name, context)
    
    def post(self, request):
        """Handle library removal using actual LibraryService.delete"""
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        library_id = request.POST.get('library_id')
        if not library_id:
            messages.error(request, '❌ No library selected')
            return redirect('libraries:remove')

        if not MHVTL_SERVICE_AVAILABLE:
            messages.error(request, "❌ MHVTL service not available")
            return redirect('libraries:remove')

        try:
            library_id = int(library_id)
            force = request.POST.get('force', '').lower() in ('true', '1', 'yes')
            remove_media = request.POST.get('remove_media', '').upper() == 'YES'

            # Use actual LibraryService.delete method
            # This removes from device.conf (the authoritative source)
            service = LibraryService()
            result = service.delete(library_id, force=force, remove_media=remove_media)

            # Handle ServiceResult exactly as defined
            if result.success:
                # Also update Django DB if library exists there
                try:
                    library = Library.objects.get(library_id=library_id)
                    library.is_active = False
                    library.save()
                    # Mark associated drives as inactive
                    library.drives.update(is_active=False)
                except Library.DoesNotExist:
                    pass  # Library wasn't in Django DB, that's fine
                
                messages.success(request, f"✅ {result.message}")

                # No restart here. The delete has already stopped this
                # library's own daemons, and no other library reads the
                # section that was removed. This used to restart
                # mhvtl.target - every library on the host - which cut off
                # any job running on the others and left every iSCSI export
                # holding devices that no longer existed.
                _sync_database(request)
                        
            else:
                # Check if force is required (tapes mounted in drives)
                if result.data and result.data.get('requires_force'):
                    messages.error(request, f"❌ {result.message}")
                    messages.warning(request, "⚠️ Unload all tapes from drives first, or use force delete")
                else:
                    messages.error(request, f"❌ Removal failed: {result.message}")
                    if hasattr(result, 'errors') and result.errors:
                        for error in result.errors:
                            messages.error(request, f"• {error}")
                    
        except ValueError:
            messages.error(request, '❌ Invalid library ID')
        except Exception as e:
            messages.error(request, f"❌ Error removing library: {str(e)}")
        
        return redirect('libraries:remove')


class ResetDefaultView(View):
    """Reset all libraries using actual service"""
    template_name = 'libraries/reset_default.html'
    
    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        
        context = {
            'title': 'Reset to MHVTL Defaults'
        }
        return render(request, self.template_name, context)
    
    def post(self, request):
        """Handle reset using actual LibraryService.delete for each library"""
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        
        confirm = request.POST.get('confirm')
        force = request.POST.get('force', '').lower() in ('true', '1', 'yes')
        if confirm == 'reset':
            if not MHVTL_SERVICE_AVAILABLE:
                messages.error(request, "❌ MHVTL service not available")
                return redirect('libraries:dashboard')

            try:
                service = LibraryService()

                # Get all active libraries to remove
                active_libraries = Library.objects.filter(is_active=True)
                library_ids = list(active_libraries.values_list('library_id', flat=True))

                success_count = 0
                error_count = 0
                blocked_libraries = []

                # Remove each library using the actual service method
                for library_id in library_ids:
                    result = service.delete(library_id, force=force)
                    if result.success:
                        success_count += 1
                    else:
                        error_count += 1
                        if result.data and result.data.get('requires_force'):
                            blocked_libraries.append(library_id)
                        messages.warning(request, f"⚠️ Failed to remove library {library_id}: {result.message}")
                
                # Mark all libraries as inactive in Django
                active_libraries.update(is_active=False)
                Drive.objects.filter(library__in=active_libraries).update(is_active=False)
                
                # Restart MHVTL services using actual method
                try:
                    restart_result = service.restart_services()
                    if restart_result.success:
                        messages.success(request, "🔄 MHVTL services restarted")
                    else:
                        messages.warning(request, f"⚠️ Service restart failed: {restart_result.message}")
                except Exception:
                    messages.warning(request, "⚠️ Service restart failed")
                
                _sync_database(request)
                
                if success_count > 0:
                    messages.success(request, f"✅ Successfully removed {success_count} libraries")
                if error_count > 0:
                    messages.error(request, f"❌ Failed to remove {error_count} libraries")
                if blocked_libraries:
                    messages.warning(request, f"⚠️ Libraries {blocked_libraries} have tapes in drives. Unload tapes first or use force delete.")
                    
            except Exception as e:
                messages.error(request, f'❌ Error during reset: {str(e)}')
            
            return redirect('libraries:dashboard')
            
        return render(request, self.template_name)


class ConfigFilesView(View):
    """View actual MHVTL configuration files"""
    template_name = 'libraries/config_files.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # Listing, allowlisting and stat-ing live in services.config.inventory;
        # this used to be ~110 lines of os.listdir and os.stat inline here, with
        # /etc/mhvtl hardcoded and the allowlist written out twice more below.
        from apps.libraries.services.config import inventory
        from apps.libraries.services.core import config_dir

        directory = config_dir()
        files_info = [entry.to_dict() for entry in inventory.list_files(directory)]
        service_status = _mhvtl_service_status()

        context = {
            'files_info': files_info,
            'config_dir': str(directory),
            'directory_exists': directory.is_dir(),
            'service_status': service_status,
            'mhvtl_available': service_status.get('available', False),
            'error_message': None if directory.is_dir() else (
                f"MHVTL configuration directory '{directory}' does not exist. "
                f'MHVTL may not be installed or configured.'),
            'title': 'MHVTL Configuration Files',
        }
        return render(request, self.template_name, context)


class DownloadConfigView(View):
    """Download one config file."""

    def get(self, request, filename):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # The allowlist and the containment check live in
        # services.config.inventory. This view used to carry its own copy -
        # the third in this module - and read straight from a hardcoded
        # /etc/mhvtl.
        from apps.libraries.services.config import inventory

        content = inventory.read_file(filename)
        if content is None:
            raise Http404('File not available for download')

        response = HttpResponse(content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class DownloadAllConfigsView(View):
    """Download every MHVTL config file as a zip archive."""

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        from apps.libraries.services.config import inventory

        try:
            archive = inventory.export_zip()
        except Exception as exc:  # noqa: BLE001 - shown to the operator
            logger.error('building the config archive: %s', exc)
            messages.error(request, f'Could not build the archive: {exc}')
            return redirect('libraries:config_files')

        response = HttpResponse(archive, content_type='application/zip')
        response['Content-Disposition'] = 'attachment; filename="mhvtl_configs.zip"'
        return response


class MHVTLStatusView(View):
    """MHVTL system status using actual service"""
    template_name = 'libraries/mhvtl_status.html'
    
    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')
        
        # From services: the directories from core, unit state from console,
        # the libraries from services/libraries. This called
        # MHVTLLibraryService._check_service_status(), which does not exist.
        from apps.libraries.services.console import units
        from apps.libraries.services.core import config_dir as configured_dir
        from apps.libraries.services.core import home_dir
        from apps.libraries.services.libraries import LibraryService

        try:
            config_dir = Path(configured_dir())
            data_dir = Path(home_dir())

            # Check MHVTL scripts
            scripts = {}
            for name in ['mktape', 'vtlcmd', 'vtllibrary', 'vtltape']:
                p = Path(f'/usr/bin/{name}')
                scripts[name] = {
                    'path': str(p),
                    'exists': p.exists(),
                    'executable': p.exists() and os.access(str(p), os.X_OK),
                }

            state = units.status()
            target_status = 'active' if state.target_active else 'inactive'
            svc_info = {'mhvtl.target': target_status,
                        **{unit.name: unit.state
                           for unit in state.libraries + state.drives}}

            errors = []
            config_issues = []

            if not config_dir.exists():
                config_issues.append(f'{config_dir} directory missing')
            if not data_dir.exists():
                config_issues.append(f'{data_dir} directory missing')
            if not state.target_active:
                config_issues.append(f'mhvtl.target is {target_status}')
            if state.stale:
                config_issues.append('units left behind by removed devices: '
                                     + ', '.join(state.stale))

            listed = LibraryService().list(with_contents=False)
            libraries_status = (listed.data or {}).get('libraries', [])
            if not listed.success:
                errors.append(listed.message)

            mhvtl_status = {
                'available': target_status == 'active',
                'config_dir_exists': config_dir.exists(),
                'data_dir_exists': data_dir.exists(),
                'scripts': scripts,
                'errors': errors,
                'service_statuses': svc_info,
            }

            context = {
                'mhvtl_status': mhvtl_status,
                'libraries_status': libraries_status,
                'config_valid': len(config_issues) == 0,
                'config_issues': config_issues,
                'title': 'MHVTL System Status'
            }

        except Exception as e:
            context = {
                'error_message': str(e),
                'title': 'MHVTL System Status'
            }

        return render(request, self.template_name, context)


class CleanupOrphanedView(View):
    """Form-based view to cleanup orphaned libraries"""
    template_name = 'libraries/cleanup_orphaned.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        context = self._get_cleanup_context()
        return render(request, self.template_name, context)

    def post(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        action = request.POST.get('action')

        if action == 'cleanup':
            # Perform database cleanup
            result = self._perform_cleanup()
            if result['success']:
                messages.success(
                    request,
                    f"Database cleanup complete: Deleted {result['deleted_libraries']} libraries "
                    f"and {result['deleted_drives']} drives."
                )
            else:
                messages.error(request, f"Database cleanup failed: {', '.join(result['errors'])}")

        elif action == 'cleanup_config':
            # Perform device.conf orphan cleanup
            if MHVTL_SERVICE_AVAILABLE:
                try:
                    result = orphans.cleanup(dry_run=False)
                    if result.success:
                        cleaned = result.data.get('cleaned', {})
                        messages.success(
                            request,
                            f"Config cleanup complete: Removed {len(cleaned.get('drives_removed', []))} drives, "
                            f"{len(cleaned.get('files_removed', []))} files, "
                            f"{len(cleaned.get('services_stopped', []))} services."
                        )
                        if result.errors:
                            for err in result.errors:
                                messages.warning(request, f"Warning: {err}")
                    else:
                        messages.error(request, f"Config cleanup failed: {result.message}")
                except Exception as e:
                    messages.error(request, f"Config cleanup error: {str(e)}")
            else:
                messages.error(request, "MHVTL service not available")

        return redirect('libraries:cleanup_orphaned')

    def _get_cleanup_context(self):
        """Get context with discovered vs database comparison"""
        context = {
            'title': 'Cleanup Orphaned Libraries',
            'mhvtl_libraries': [],
            'database_libraries': [],
            'orphaned_libraries': [],
            'discovery_available': False,   # there is no discovery app
            'error_message': None,
            # New: device.conf orphans
            'config_orphans': None,
            'mhvtl_service_available': MHVTL_SERVICE_AVAILABLE,
        }

        # device.conf orphans, from services/libraries/orphans
        if MHVTL_SERVICE_AVAILABLE:
            try:
                context['config_orphans'] = orphans.find()
            except Exception as e:
                context['config_orphans_error'] = str(e)

        # Get libraries from database
        db_libraries = Library.objects.filter(is_active=True).select_related('brand', 'model')
        context['database_libraries'] = list(db_libraries)

        if not MHVTL_SERVICE_AVAILABLE:
            context['error_message'] = 'MHVTL service not available'
            return context

        try:
            live_libraries = _configured_libraries()

            mhvtl_library_ids = {lib.library_id for lib in live_libraries}
            mhvtl_libraries = [
                {
                    'library_id': lib.library_id,
                    'vendor': lib.vendor,
                    'product': lib.product,
                    'serial': lib.serial,
                    'channel': lib.channel,
                    'target': lib.target,
                    'lun': lib.lun,
                }
                for lib in live_libraries
            ]

            context['mhvtl_libraries'] = mhvtl_libraries
            context['mhvtl_library_ids'] = mhvtl_library_ids

            # Find orphaned libraries (in DB but not in MHVTL config)
            orphaned = []
            for lib in db_libraries:
                if lib.library_id not in mhvtl_library_ids:
                    orphaned.append({
                        'library': lib,
                        'drives_count': lib.drives.filter(is_active=True).count()
                    })

            context['orphaned_libraries'] = orphaned
            context['orphaned_count'] = len(orphaned)

            # Count drives that would be deleted
            total_orphaned_drives = sum(o['drives_count'] for o in orphaned)
            context['orphaned_drives_count'] = total_orphaned_drives

        except Exception as e:
            context['error_message'] = f"Error reading MHVTL config: {str(e)}"

        return context

    def _perform_cleanup(self):
        """Delete the database rows for libraries device.conf no longer has.

        This called a discovery service that does not exist, so the page
        always answered "Discovery service not available". device.conf is the
        authority; a row for a library it does not declare is stale.
        """
        from apps.libraries.services.libraries import LibraryService

        result = {'success': True, 'deleted_libraries': 0, 'deleted_drives': 0,
                  'errors': []}

        listed = LibraryService().list(with_contents=False)
        if not listed.success:
            # An unreadable device.conf must not read as "no libraries", which
            # would delete every row.
            return {**result, 'success': False, 'errors': [listed.message]}

        declared = {library['library_id'] for library in listed.data['libraries']}
        orphaned = Library.objects.exclude(library_id__in=declared)
        result['deleted_drives'] = Drive.objects.filter(library__in=orphaned).count()
        result['deleted_libraries'] = orphaned.count()
        orphaned.delete()                       # drives cascade
        return result
