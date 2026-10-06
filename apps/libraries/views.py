# apps/libraries/views.py - library pages, on apps/libraries/services
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.http import urlencode
from django.views import View
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
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
from apps.libraries.services.libraries import presets as library_presets
from apps.libraries.services.libraries import validation as library_validation
from apps.libraries.services.profiles import personalities
# get_profile_options and MEDIA_SUFFIX were imported here to compose the setup
# form's embedded JSON. libraries/setup_form composes the form now, and the
# page renders it, so neither reaches this module any more.
from apps.libraries.services.profiles.data import get_profile

# Kept for the templates and branches that still test them; the services are
# part of this app and always importable.
MHVTL_SERVICE_AVAILABLE = True

#: How many backups the cleanup page lists. Every write takes one and nothing
#: pruned them, so this host had 949: rendering them all made the page 867 KB
#: with 949 Remove buttons. The service still returns them all - the CLI and the
#: JSON want that - and the page shows the newest and offers the prune.
BACKUPS_SHOWN = 25
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

        from apps.libraries.services.sync.service import sync_mhvtl_to_django

        result = sync_mhvtl_to_django()
        if not result.success:
            return 0, result.message
        stats = result.data
        return stats['created'] + stats['activated'] + stats['deactivated'], None

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
            # And the strip, from the service, in the words the terminal uses.
            'summary': _host_summary(),
        })

        return render(request, self.template_name, context)


class SetupChoiceView(View):
    """Setup workflow - choose between standard, custom, and a saved preset."""
    template_name = 'libraries/setup_choice.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        # The same presets `mhvtl preset list` shows, because it is
        # the same service call. Offered as a third way in only when there
        # are some: a card listing nothing teaches nothing.
        saved = library_presets.names()
        context = {
            'title': 'Choose Setup Method',
            'presets': ([row for row in saved.data['presets'] if row['complete']]
                        if saved.success else []),
            'presets_path': saved.data['path'] if saved.success else '',
        }
        if not saved.success:
            messages.warning(request, saved.message)
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
        elif setup_type == 'preset':
            return self._from_preset(request)
        else:
            messages.error(request, 'Please select a setup method.')
            return self.get(request)

    def _from_preset(self, request):
        """A saved configuration names its own vendor, so the catalogue step
        is skipped: this opens that vendor's form with the preset applied.

        The refusal is the service's, fixes included - the same words the
        command line prints for the same preset.
        """
        name = request.POST.get('preset', '').strip()
        if not name:
            messages.error(request, 'Please choose a saved configuration.')
            return self.get(request)

        resolved = library_presets.resolve(name)
        if not resolved.success:
            messages.error(request, resolved.message)
            for fix in resolved.errors:
                messages.info(request, fix)
            return self.get(request)

        profile = resolved.data['spec']['profile']
        url = reverse('libraries:brand_config',
                      kwargs={'brand_name': profile})
        return redirect(f'{url}?{urlencode({"preset": name})}')


def _brand_catalogue():
    """Every vendor the application can create, from the profiles.

    The profiles are the authority: they are what validation, the create form
    and MHVTL itself agree on. This page used to list LibraryBrand rows, and
    that table had grown case-duplicated pairs (Dell and DELL, Spectra and
    SPECTRA, ...) and a TestVendor left by a test run, while a profile with no
    row would not have appeared at all.

    Composed by services/profiles/catalogue, which is what `mhvtl profile
    list` reads as well. This function walked the profile tables itself until
    4 October 2026 - the same composition DriveService.models_for_profile was
    doing for the terminal - and now adds only what these cards need on top:
    the family grouping, the newest LTO generation, and the layout titles.
    """
    from apps.libraries.services.profiles import catalogue as profiles_catalogue

    cards = []
    for row in profiles_catalogue.summaries():
        described = profiles_catalogue.describe(row['profile'])
        densities = row['densities']
        cards.append({
            'key': row['profile'],
            'display': row['vendor'],
            'models': row['library_models'],
            'model_count': row['model_count'],
            'drive_count': row['drive_count'],
            'media': densities,
            'families': sorted({_media_family(m) for m in densities}),
            'newest_lto': _newest_lto(densities),
            'default_model': row['default_model'],
            'layouts': sorted({model['layout'] for model in described['models']
                               if model['layout']}),
        })
    return cards


def _media_family(density: str) -> str:
    """Which family a density belongs to, for grouping the cards.

    The rule is the service's - this page had its own copy until the terminal
    needed the same grouping for `profile show`. Kept as a one-line wrapper
    because the templates and the two views below read better for it.
    """
    from apps.libraries.services.profiles import catalogue

    return catalogue.media_family(density)


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


def _asked(source):
    """The choices a form state is composed from, out of GET or POST.

    Each one is a dropdown the operator changed, except `wanted_media`, which
    the vendor page's filter links also carry. setup_form.state ignores
    anything this vendor cannot do rather than refusing it, so a stale link
    opens the page.

    One name per question, and ``wanted_media`` is the name worth explaining:
    the filter was called ``media`` too, and a cartridge row is
    ``media=LTO8:20``. QueryDict.get returns the LAST value, so the row
    answered the filter - the wanted tape arrived as 'LTO8:20', no vendor
    takes a density by that name, and choosing a tape at the top of the form
    quietly stopped narrowing the drives below it. The vendor list keeps
    ``?media=`` for its own filtering; it has no rows to collide with.
    """
    asked = {
        'wanted_media': (source.get('wanted_media') or '').strip().upper(),
        'library_model': (source.get('library_model') or '').strip(),
        # One row per kind, in the order the page has them, as MODEL:COUNT -
        # the same spelling `--drive` takes, parsed by the same service
        # (libraries.spec.parse_runs). getlist, because the rows repeat.
        'drive_runs': [run for run in source.getlist('drive') if run.strip()],
        'media_runs': [run for run in source.getlist('media') if run.strip()],
    }
    # The empty slots are a single number and stay one. The drive and
    # cartridge counts live in their rows now: they used to be sent on their
    # own, which is why a preset holding two kinds lost its counts to the
    # profile's defaults.
    if (source.get('empty_slots') or '').strip():
        asked['empty_slots'] = source['empty_slots'].strip()
    # "Add another kind": a word, because what a new row may hold is the
    # catalogue's answer - whatever the rows above have not taken.
    if source.get('add') in ('drive', 'media'):
        asked['add'] = source['add']
    return asked


def setup_form_ajax(request, brand_name):
    """The form after a change, rendered - the page swaps the parts in.

    The same view and the same template as a fresh page, so a changed
    dropdown, an added row and a freshly opened page are all answered by one
    piece of code. It returned JSON until the rows arrived: with one choice
    per question the script could map values onto fields, but a row carries
    its own options, its own marks and its own sentence, and a script that
    built those from JSON would be composing the page again - which is the
    thing this form was rebuilt to stop.

    It costs a whole page per change on a console served over a LAN, and
    buys one template with nothing to keep in sync with a second one.
    """
    if not request.session.get('mhvtl_logged_in'):
        return JsonResponse({'success': False, 'error': 'Not authenticated'},
                            status=401)
    return BrandConfigView.as_view()(request, brand_name=brand_name)


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


def _host_summary():
    """What this host holds, for the strip above a page's libraries.

    ``LibraryService.summary`` reads device.conf and the contents files, and
    ``mhvtl library list`` ends with the same sentence. None of it is
    composed here: a view that did its own counting is what the panel this
    replaces was - five numbers from context keys nobody set.

    None when it cannot be read, because the partial shows nothing rather
    than zeros. Zeros were the bug.

    Whether MHVTL is running travels with the counts. It was a badge beside a
    "System Overview" heading on the landing page, over three tiles that
    said what this strip says - and it was on that one page only, while the
    counts are on both.
    """
    from apps.libraries.services.libraries import LibraryService

    try:
        result = LibraryService().summary()
    except Exception:                       # noqa: BLE001 - the page stands
        logger.warning('reading the host summary', exc_info=True)
        return None
    if not result.success:
        return None

    status = _mhvtl_service_status()
    return {**result.data,
            'mhvtl_running': bool(status.get('running')),
            'mhvtl_known': bool(status.get('available'))}


def _sync_database(request):
    """Bring the database back in line with device.conf, and say so.

    A rendering of the service's answer, not a second copy of it: the sync
    reports its own refusal now, where it used to raise and leave each of
    nine callers to invent the handling.
    """
    from apps.libraries.services.sync.service import sync_mhvtl_to_django

    result = sync_mhvtl_to_django()
    if not result.success:
        logger.warning('syncing the database with device.conf: %s',
                       result.message)
        messages.warning(request, f'The database was not updated: '
                                  f'{result.message}')
        return None
    messages.info(request, f'Database updated from device.conf: '
                           f'{result.message}')
    return result.data


#: _free_targets() was here until 4 October 2026: it read device.conf and
#: asked config/ids how many SCSI targets were left, so that the form could
#: cap its drive count by the host rather than by the model's element layout.
#: It was the best thing on the page and the terminal never had it, because a
#: helper in views.py is reachable only from the web. It is
#: libraries/setup_form._limits now, where `--interactive` asks it too.


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

        profile_key = brand.name.upper()

        # ?preset=NAME applies a saved configuration to this form. Resolved
        # here, by the same call `mhvtl library create --preset NAME` makes,
        # so the two cannot offer different things; the operator can still
        # change every field before creating.
        chosen, chosen_name = {}, request.GET.get('preset', '').strip()
        if chosen_name:
            resolved = library_presets.resolve(chosen_name)
            if resolved.success:
                chosen = resolved.data['spec']
                messages.info(request,
                              f"Starting from the saved configuration "
                              f"'{chosen_name}'.")
            else:
                messages.warning(request, resolved.message)
                for fix in resolved.errors:
                    messages.info(request, fix)

        # The presets this vendor has, which is what makes them findable
        # without reading the file.
        offered = library_presets.for_profile(profile_key)

        # The whole form, decided by the service: every dropdown's options
        # with the selected one marked, the counts, the limits this host can
        # actually give it, the serial and barcode creation will write, and
        # the sentences. This block used to compose a JSON profile for the
        # page's script to narrow for itself - which is how the page came to
        # filter on what a drive loads where the services mean what it writes,
        # and to choose a half-height HH9 for LTO-9 where the services choose
        # a TD9. See services/libraries/setup_form.
        from apps.libraries.services.libraries import setup_form

        state = setup_form.state(profile_key, preset=chosen,
                                 **_asked(request.GET))
        if not state.success:
            messages.warning(request, state.message)
        form = state.data or {}

        context = {
            'brand': brand,
            'models': models,
            'profile_key': profile_key,
            'form': form,
            'next_library_id': form.get('library_id'),
            # Channel is always 0 and the target is allocated at write time
            # against device.conf as it stands, so neither is asked for.
            'next_channel': 0,
            'next_lun': 0,
            'presets': offered.data['presets'] if offered.success else [],
            'preset_name': chosen_name if chosen else '',
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

            barcode_prefix = request.POST.get('barcode_prefix', 'E01').upper()
            map_count = request.POST.get('map_count', '')

            # The specification is the one the form was SHOWING, built by the
            # same service call that rendered it. The alternative - reading
            # the posted fields a second time here - is a second reading of
            # the same inputs, free to differ from what the operator saw;
            # that is how four drives and fifty cartridges came out of a
            # preset asking for four and thirty.
            from apps.libraries.services.libraries import setup_form

            shown = setup_form.state(profile_key, library_id=library_id,
                                     **_asked(request.POST))
            if not shown.success:
                messages.error(request, shown.message)
                return redirect('libraries:brand_config', brand_name=brand_name)
            form = shown.data

            if not form['library_model']['selected']:
                messages.error(request, "❌ Please select a library model")
                return redirect('libraries:brand_config', brand_name=brand_name)
            if not any(row['selected'] for row in form['drives']['rows']):
                messages.error(request, "❌ Please select a drive model")
                return redirect('libraries:brand_config', brand_name=brand_name)
            if not any(row['selected'] for row in form['media']['rows']):
                messages.error(request, "❌ Please select a media type")
                return redirect('libraries:brand_config', brand_name=brand_name)

            library_data = {
                **setup_form.as_spec(profile_key, library_id,
                                     form['library_model']['selected'],
                                     form['drives'], form['media'],
                                     form['counts']),
                # Only when the form sends one. An absent or empty field is a
                # question for the service, which answers it the same way for
                # the terminal (profiles.personalities.device_serial); the
                # default here was `XYZZY_{library_id}` while the page's own
                # script posted `library_id + 80000000`, so one form had two
                # answers and neither was the service's.
                **({'serial': request.POST['unit_serial_number'].strip()}
                   if request.POST.get('unit_serial_number', '').strip()
                   else {}),
                'barcode_prefix': barcode_prefix,
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

            # The database record is step 6 of the workflow above, reported
            # with the other steps, so it happens for the CLI too. It used to
            # be here, which is why a library created from the CLI was
            # configured, running, and invisible in every dropdown.

            self._keep_preset(request, library_data)

            return redirect('libraries:dashboard')

        except Exception as e:
            messages.error(request, f'❌ Unexpected error creating library: {str(e)}')
            return redirect('libraries:brand_config', brand_name=brand_name)


    @staticmethod
    def _keep_preset(request, library_data):
        """Save this configuration under a name, if one was asked for.

        `--save-preset NAME` on the command line, a field on this form, one
        service call either way: config.presets.savable decides what is kept
        and libraries.presets.save writes it. Only reached after a successful
        create, for the same reason the CLI only saves then - a preset that
        recreates a failure is worse than no preset.
        """
        name = request.POST.get('save_preset', '').strip()
        if not name:
            return

        from apps.libraries.services.config.presets import savable

        kept = library_presets.save(name, savable(library_data))
        if kept.success:
            messages.success(request, kept.message)
            for warning in kept.data.get('warnings', []):
                messages.warning(request, warning)
        else:
            messages.warning(request, f'The library was created, but the '
                                      f'preset was not saved: {kept.message}')
            for fix in kept.errors:
                messages.info(request, fix)


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
                # As on the brand form: only what was given. The service
                # answers an empty one, and the same way for both front ends.
                **({'serial': request.POST['serial'].strip()}
                   if request.POST.get('serial', '').strip() else {}),
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
            # What the host holds, from the service that reads device.conf.
            # The panel this replaces read the database and showed zeros.
            'summary': _host_summary(),
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

    from apps.libraries.services.sync.service import sync_mhvtl_to_django

    synced = sync_mhvtl_to_django()
    if not synced.success:
        # Falls through to the 404, which is the honest answer: the page was
        # asked for a library the database does not have, and the one file
        # that could say otherwise could not be read.
        logger.warning('sync while opening library %s: %s', library_id,
                       synced.message)
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
                # with_ltfs: this page's tiles carry the LTFS chips, so it
                # is one of the two callers that asks. Gates in the
                # service mean it costs nothing on a library whose drives
                # LTFS cannot open.
                listed = TapeService().list(library_id, with_ltfs=True)
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

        elif action == 'full_scan':
            self._full_scan(request)

        elif action == 'remove_backup':
            self._remove_backup(request, request.POST.get('name'))

        elif action == 'prune_backups':
            self._prune_backups(request, request.POST.get('keep'))

        return redirect('libraries:cleanup_orphaned')

    @staticmethod
    def _full_scan(request):
        """Reconcile the whole database against device.conf.

        The same sync the library list page's Full Scan button runs, as a form
        post rather than through fetch(): this page is server-rendered with
        forms and messages, and the decision is the service's either way.

        It belongs here because this is the page for putting the database right,
        and because a whole-configuration reconcile is an explicit repair rather
        than a step inside another operation - see the one-way rule in
        guides/architecture.rst. Deactivating rows device.conf does not declare
        is what it is for, not a side effect to be guarded against.
        """
        from apps.libraries.services.sync.service import sync_mhvtl_to_django

        try:
            result = sync_mhvtl_to_django()
        except Exception as exc:                       # noqa: BLE001 - shown
            logger.exception('running a full scan')
            messages.error(request, f'Full scan failed: {exc}')
            return
        if not result.success:
            # device.conf could not be READ - which is not the same as saying
            # there are no libraries, and the sync refuses rather than
            # emptying the database. A refusal, not a failure: it is reported
            # rather than raised, so this is a branch and not an except.
            messages.error(request, f'Full scan refused: {result.message}')
            for detail in result.errors:
                messages.info(request, detail)
            return
        stats = result.data

        # The sentence is the service's: this page, two AJAX endpoints and
        # the command line each composed one from the same counts, and two of
        # them said nearly the same thing in different words.
        messages.success(request, f'Full scan complete: {result.message}')
        messages.info(request,
                      f'The database now holds {stats["total_db"]} active '
                      f'library/libraries and {stats["total_drives"]} drive(s)')

    @staticmethod
    def _remove_backup(request, name):
        """Delete one backup. The service holds it to backups/ by refusing any
        name with a separator in it, so nothing here needs to check the path."""
        from apps.libraries.services.config.service import ConfigService

        result = ConfigService().remove_backup(name or '')
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message)
            for error in result.errors:
                messages.error(request, f'  {error}')

    @staticmethod
    def _prune_backups(request, keep):
        """Delete all but the newest N. The count comes from the operator and is
        never defaulted: a prune that deletes everything because a field was
        empty is not a prune."""
        from apps.libraries.services.config.service import ConfigService

        try:
            wanted = int(keep)
        except (TypeError, ValueError):
            messages.error(request, 'How many backups to keep must be a number')
            return
        result = ConfigService().prune_backups(keep=wanted)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message)
            for error in result.errors:
                messages.error(request, f'  {error}')

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
            # The backups every config write takes, which nothing used to show:
            # 949 of them had collected here before this page could list them.
            'backups': [],
            'backups_count': 0,
            'backups_hidden': 0,
            'backups_total_bytes': 0,
            'backups_path': '',
        }

        # device.conf orphans, from services/libraries/orphans
        if MHVTL_SERVICE_AVAILABLE:
            try:
                context['config_orphans'] = orphans.find()
            except Exception as e:
                context['config_orphans_error'] = str(e)

            # Backups belong on this page because this is where an operator
            # comes to tidy up, and because orphans.cleanup() takes one before
            # it removes anything - the copy it leaves is the way back.
            try:
                from apps.libraries.services.config.service import ConfigService

                listed = ConfigService().backups()
                if listed.success:
                    # The newest few only. Rendering all of them made this page
                    # 867 KB of HTML and 949 Remove buttons - an operator with
                    # that many is looking for the prune, not for row 700.
                    rows = listed.data['backups']
                    context['backups'] = rows[:BACKUPS_SHOWN]
                    context['backups_count'] = listed.data['count']
                    context['backups_hidden'] = max(
                        len(rows) - BACKUPS_SHOWN, 0)
                    context['backups_total_bytes'] = listed.data['total_bytes']
                    context['backups_path'] = listed.data['path']
                else:
                    context['backups_error'] = listed.message
            except Exception as e:                     # noqa: BLE001 - shown
                context['backups_error'] = str(e)

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


class AboutView(View):
    """What this is, which version it is, and what it is running on.

    The page a bug report is written from. It decides nothing: every value
    comes from services.about, which `mhvtl --version` and
    `mhvtl status system` also call, so the page and the terminal cannot
    disagree about what is installed - which they did, for twenty minutes,
    when the footer said 2.0.0 after 2.1.0 went on.

    Login required, like every other page here. The version is not a secret,
    but there is no reason for an unauthenticated visitor to be told which
    release to look up.
    """
    template_name = 'libraries/about.html'

    def get(self, request):
        if not request.session.get('mhvtl_logged_in'):
            return redirect('authentication:login')

        from apps.libraries.services import about

        result = about.facts()
        return render(request, self.template_name, {
            'title': 'About',
            'project': result.data['project'],
            'runtime': result.data['runtime'],
        })
