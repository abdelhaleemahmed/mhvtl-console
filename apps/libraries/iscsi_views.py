"""
iSCSI Views
Django views for iSCSI target management using LIO/targetcli.

Every view calls services/iscsi directly (and services/config for the SCSI
addresses on the export page). The helpers below only shape the answers into
what the templates and their JavaScript read; that used to be
adapters/iscsi_service.py.

Location: apps/libraries/iscsi_views.py
"""

import json
import logging

from django.shortcuts import render, redirect
from django.views import View
from django.contrib import messages
from django.http import JsonResponse

from .models import Library
from .post_only import JsonOnGet, RedirectOnGet
from .services.config.service import ConfigService
from .services.iscsi import IscsiService, bindings

#: Kept for the templates, which show a banner when it is False. The service
#: is part of this application now, so it always is.
ISCSI_SERVICE_AVAILABLE = True

logger = logging.getLogger(__name__)


# ============================================================================
# Helper Functions
# ============================================================================

def check_login(request):
    """Check if user is logged in"""
    return request.session.get('mhvtl_logged_in', False)


def _listed(result, key):
    """The list a reading call returned, or [] (logged) when it failed."""
    if not result.success:
        logger.warning('iSCSI: %s', result.message)
        return []
    return (result.data or {}).get(key, [])


def _targets(service):
    """Every target, as the dicts the templates read.

    Each one is told which library it exports, when it is one library's
    export, so the page can offer to remove that library's devices with it.
    Worked out from the backstores its LUNs use, in the service - the page
    does not read meaning out of a target's name.
    """
    from .services.iscsi import workflow

    targets = _listed(service.targets(), 'targets')
    for target in targets:
        target['library_id'] = workflow.library_of(target)
    return targets


def _backstores(service):
    return _listed(service.backstores(), 'backstores')


def _devices(service):
    """The MHVTL devices that can be exported."""
    return _listed(service.available_devices(), 'devices')


def _target(service, iqn):
    return next((t for t in _targets(service) if t.get('iqn') == iqn), None)


def _service_state(running: bool, enabled: bool) -> dict:
    return {'running': running, 'enabled': enabled,
            'active_state': 'active' if running else 'inactive',
            'sub_state': 'running' if running else 'dead'}


def _status(service) -> dict:
    """The dashboard's answer: service state, targets, backstores."""
    from django.utils import timezone

    result = service.status()
    data = result.data or {} if result.success else {}
    if not result.success:
        logger.warning('reading iSCSI status: %s', result.message)
    unit = data.get('service', {})
    targets = data.get('targets', [])
    backstores = data.get('backstores', [])
    return {
        'service': _service_state(bool(unit.get('running')), bool(unit.get('enabled'))),
        'targets': targets,
        'backstores': backstores,
        'config_saved': bool(data.get('config_saved')),
        'target_count': len(targets),
        'backstore_count': len(backstores),
        'last_checked': timezone.now().isoformat(),
    }


def _library_scsi_targets() -> dict:
    """{library_id: [SCSI target of its changer, then of each drive]}.

    The export page filters the device list per library with it.
    """
    conf = ConfigService().device_conf()
    if conf is None:
        return {}
    mapping = {}
    for library_id, library in sorted(conf.libraries.items()):
        targets = [library.get('target')]
        targets += [drive.get('target')
                    for _, drive in sorted(conf.drives_of(library_id).items())]
        mapping[library_id] = targets
    return mapping


def _not_authenticated():
    return JsonResponse({'success': False, 'error': 'Not authenticated'}, status=401)


# ============================================================================
# iSCSI Dashboard View
# ============================================================================

class IscsiDashboardView(View):
    """
    iSCSI Dashboard - Overview of targets, backstores, and service status.
    """
    template_name = 'libraries/iscsi/dashboard.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        context = {
            'iscsi_status': _status(IscsiService()),
            'exports': _exports_by_library(),
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': 'iSCSI Management'
        }

        return render(request, self.template_name, context)


def _exports_by_library():
    """Exported devices grouped by library, each with whether it still answers.

    A daemon restart leaves an export on a deleted device: the target logs
    initiators in and shows them no LUNs. See services/iscsi/bindings.py.
    """
    if not ISCSI_SERVICE_AVAILABLE:
        return None
    try:
        result = IscsiService().binding_status()
    except Exception as exc:                           # noqa: BLE001 - shown
        logger.warning('checking iSCSI bindings: %s', exc)
        return {'error': str(exc), 'libraries': []}
    if not result.success:
        return {'error': '; '.join(result.errors) or result.message, 'libraries': []}
    grouped = {}
    for binding in result.data['bindings']:
        grouped.setdefault(binding['library_id'], []).append(binding)
    libraries = [{'library_id': library_id, 'bindings': items,
                  'needs_rebind': library_id is not None and any(
                      b['status'] in ('stale', 'unknown', 'withheld') for b in items)}
                 for library_id, items in sorted(grouped.items(),
                                                 key=lambda kv: (kv[0] is None, kv[0] or 0))]
    return {'error': None, 'libraries': libraries,
            'stale': sum(b['status'] in ('stale', 'missing', 'withheld')
                         for b in result.data['bindings'])}


class IscsiRebindView(RedirectOnGet, View):
    """Bind a library's exported devices to the daemons running now.

    The iSCSI page's form, not a page: a GET goes back there.
    """

    page = 'libraries:iscsi_dashboard'

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')
        try:
            library_id = int(request.POST.get('library_id', ''))
        except ValueError:
            messages.error(request, 'Choose a library to rebind')
            return redirect('libraries:iscsi_dashboard')

        result = IscsiService().rebind_library(library_id)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, f"{result.message}: {'; '.join(result.errors)}")
        return redirect('libraries:iscsi_dashboard')


class IscsiRecordBindingView(RedirectOnGet, View):
    """Record a binding for a backstore the GUI did not create.

    A backstore made with targetcli works, but the GUI has no record of which
    daemon start it bound to, so it reads *unknown* rather than *bound* and
    the boot-time check cannot tell whether it is still right. Recording says
    "this one is correct as it stands"; it changes nothing in the target.
    """

    page = 'libraries:iscsi_dashboard'

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        names = [name for name in request.POST.getlist('name') if name.strip()]
        result = bindings.record(names or None)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, f"{result.message}: {'; '.join(result.errors)}")
        return redirect('libraries:iscsi_dashboard')


# ============================================================================
# iSCSI Guide View
# ============================================================================

class IscsiGuideView(View):
    """
    iSCSI Export Guide - Step-by-step instructions for exporting libraries.
    """
    template_name = 'libraries/iscsi/guide.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        context = {
            'title': 'iSCSI Export Guide'
        }

        return render(request, self.template_name, context)


# ============================================================================
# Target Management Views
# ============================================================================

class IscsiTargetsView(View):
    """
    List all iSCSI targets.
    """
    template_name = 'libraries/iscsi/targets.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        context = {
            'targets': _targets(IscsiService()),
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': 'iSCSI Targets'
        }

        return render(request, self.template_name, context)


class IscsiCreateTargetView(View):
    """
    Create a new iSCSI target.
    """
    template_name = 'libraries/iscsi/create_target.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        # Get backstores for LUN selection
        context = {
            'backstores': _backstores(IscsiService()),
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': 'Create iSCSI Target'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        service = IscsiService()

        iqn = request.POST.get('iqn', '').strip()

        if not iqn:
            messages.error(request, "IQN is required")
            return redirect('libraries:iscsi_create_target')

        result = service.create_target(iqn)

        if result.success:
            # Handle additional options
            allow_all = request.POST.get('allow_all_initiators') == 'on'
            if allow_all:
                service.set_generate_node_acls(iqn, True)

            # targetcli gives every new target a 0.0.0.0:3260 portal by itself
            # (auto_add_default_portal), so the box used to change nothing when
            # unticked and fail to create a second one when ticked. Unticked
            # now means no portal: add the one wanted on the target's page.
            add_portal = request.POST.get('add_default_portal') == 'on'
            if not add_portal:
                removed = service.delete_portal(iqn, '0.0.0.0', 3260)
                if not removed.success:
                    messages.warning(request, f"The default portal was left in place: "
                                              f"{removed.message}")

            messages.success(request, f"Created target: {iqn}")
            return redirect('libraries:iscsi_target_detail', iqn=iqn)
        else:
            messages.error(request, f"Failed to create target: {result.message}")
            return redirect('libraries:iscsi_create_target')


class IscsiTargetDetailView(View):
    """
    View and manage a specific iSCSI target (LUNs, ACLs, portals).
    """
    template_name = 'libraries/iscsi/target_detail.html'

    def get(self, request, iqn):
        if not check_login(request):
            return redirect('authentication:login')

        service = IscsiService()
        target = _target(service, iqn)
        if not target:
            messages.error(request, f"Target {iqn} not found")
            return redirect('libraries:iscsi_targets')

        context = {
            'target': target,
            'iqn': iqn,
            'backstores': _backstores(service),
            'local': _attached_here(iqn),
            'local_initiator': _local_initiator(),
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': f'Target: {iqn}'
        }

        return render(request, self.template_name, context)


def _local_initiator():
    from .services.iscsi import initiator
    return initiator.local_name()


def _attached_here(iqn):
    """Whether this host is logged in to the target, and what it sees if so."""
    from .services.iscsi import initiator
    try:
        session = initiator.session_for(iqn)
        if session is None:
            return {'attached': False, 'devices': []}
        meaning = initiator.exported_as(iqn)
        devices = initiator.attached_devices(iqn)
        for device in devices:
            what = meaning.get(device['lun'])
            device['is'] = (f"library {what['library_id']} changer" if what and
                            what['role'] == 'changer' else
                            f"library {what['library_id']} drive {what['device_id']}"
                            if what else '')
        return {'attached': True, 'host': session['host'],
                'devices': sorted(devices, key=lambda d: d['lun'])}
    except Exception as exc:                           # noqa: BLE001 - shown
        logger.warning('checking the iSCSI sessions: %s', exc)
        return {'attached': False, 'devices': [], 'error': str(exc)}


class IscsiAttachView(RedirectOnGet, View):
    """Log this host in to one of its own targets, or out again.

    The targets page's form. Its own URL carries the iqn; the page does not,
    so the redirect drops it.
    """

    page = 'libraries:iscsi_targets'

    def page_arguments(self, request, *args, **kwargs):
        return (), {}

    def post(self, request, iqn):
        if not check_login(request):
            return redirect('authentication:login')
        from .services.iscsi import initiator
        action = request.POST.get('action')
        try:
            if action == 'attach':
                result = initiator.attach(iqn)
            elif action == 'detach':
                result = initiator.detach(iqn)
            else:
                messages.error(request, f'Unknown action: {action}')
                return redirect('libraries:iscsi_target_detail', iqn=iqn)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('libraries:iscsi_target_detail', iqn=iqn)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, f"{result.message}: {'; '.join(result.errors)}")
        return redirect('libraries:iscsi_target_detail', iqn=iqn)


# ============================================================================
# Backstore Management Views
# ============================================================================

class IscsiBackstoresView(View):
    """
    List and manage backstores.
    """
    template_name = 'libraries/iscsi/backstores.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        service = IscsiService()
        context = {
            'backstores': _backstores(service),
            'available_devices': _devices(service),
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': 'iSCSI Backstores'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        service = IscsiService()

        action = request.POST.get('action')

        if action == 'create':
            name = request.POST.get('name', '').strip()
            device_path = request.POST.get('device_path', '').strip()
            plugin = request.POST.get('plugin', 'pscsi')

            if not name or not device_path:
                messages.error(request, "Name and device path are required")
                return redirect('libraries:iscsi_backstores')

            if plugin == 'pscsi':
                result = service.create_backstore(device_path, name, plugin='pscsi')
            elif plugin == 'block':
                result = service.create_backstore(device_path, name, plugin='block')
            else:
                messages.error(request, f"Unsupported plugin type: {plugin}")
                return redirect('libraries:iscsi_backstores')

            if result.success:
                messages.success(request, f"Created backstore: {name}")
            else:
                messages.error(request, f"Failed to create backstore: {result.message}")

        elif action == 'delete':
            name = request.POST.get('name')
            plugin = request.POST.get('plugin')

            if name and plugin:
                result = service.delete_backstore(plugin, name)
                if result.success:
                    messages.success(request, f"Deleted backstore: {name}")
                else:
                    messages.error(request, f"Failed to delete backstore: {result.message}")

        return redirect('libraries:iscsi_backstores')


# ============================================================================
# Quick Export View
# ============================================================================

class IscsiExportLibraryView(View):
    """
    Quick export wizard - Export an entire library over iSCSI.
    """
    template_name = 'libraries/iscsi/export_library.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        libraries = Library.objects.filter(is_active=True).select_related('brand', 'model')

        service = IscsiService()
        available_devices = _devices(service)
        existing_targets = [t.get('iqn') for t in _targets(service)]

        # Find which library IDs are already exported
        exported_ids = set()
        for iqn in existing_targets:
            if ':library' in iqn:
                try:
                    exported_ids.add(int(iqn.split(':library')[-1]))
                except ValueError:
                    pass

        # Only show libraries that aren't already exported
        unexported_libraries = [l for l in libraries if l.library_id not in exported_ids]

        # Build a mapping of library_id → list of SCSI target numbers
        # so the template JS can filter devices per library
        try:
            library_scsi_targets = _library_scsi_targets()
        except Exception:  # noqa: BLE001 - the page still works unfiltered
            logger.exception('reading SCSI targets for the export page')
            library_scsi_targets = {}

        context = {
            'libraries': unexported_libraries,
            'exported_count': len(exported_ids),
            'available_devices': available_devices,
            'existing_targets': existing_targets,
            'library_scsi_targets_json': json.dumps(library_scsi_targets),
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': 'Export Library via iSCSI'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        service = IscsiService()

        library_id = request.POST.get('library_id')
        iqn = request.POST.get('iqn', '').strip()
        allow_all = request.POST.get('allow_all_initiators') == 'on'
        initiator_iqn = request.POST.get('initiator_iqn', '').strip()

        if not library_id:
            messages.error(request, "Please select a library")
            return redirect('libraries:iscsi_export_library')

        # The ticked devices. Their backstores are named by the workflow,
        # lib<L>_changer and lib<L>_drive<N>: the form used to post a name for
        # every device on the host and pair them with the ticked paths by
        # position, so a library's changer went out under another library's
        # device name - and remap only repoints names in that convention.
        devices = [{'device_path': path}
                   for path in request.POST.getlist('device_paths')]

        if not devices:
            messages.error(request, "No devices selected for export")
            return redirect('libraries:iscsi_export_library')

        result = service.export_library(
            library_id=int(library_id),
            devices=devices,
            iqn=iqn if iqn else None,
            allow_all_initiators=allow_all,
            initiator_iqn=initiator_iqn if not allow_all and initiator_iqn else None
        )

        if result.success:
            messages.success(request, f"Library exported as {result.data.get('iqn')}")
            return redirect('libraries:iscsi_target_detail', iqn=result.data.get('iqn'))
        else:
            error_msgs = ', '.join(result.errors) if result.errors else result.message
            messages.error(request, f"Export failed: {error_msgs}")
            return redirect('libraries:iscsi_export_library')


# ============================================================================
# Service Control View
# ============================================================================

class IscsiServiceView(View):
    """
    Control target.service (start/stop/restart/enable/disable).
    """
    template_name = 'libraries/iscsi/service.html'

    def get(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        context = {
            'service_status': _status(IscsiService())['service'],
            'iscsi_service_available': ISCSI_SERVICE_AVAILABLE,
            'title': 'iSCSI Service Control'
        }

        return render(request, self.template_name, context)

    def post(self, request):
        if not check_login(request):
            return redirect('authentication:login')

        service = IscsiService()

        action = request.POST.get('action')

        if action in ('start', 'stop', 'restart', 'enable', 'disable'):
            result = service.service(action)
        elif action == 'save_config':
            result = service.save_config()
        elif action == 'restore_config':
            result = service.restore_config()
        else:
            messages.error(request, f"Unknown action: {action}")
            return redirect('libraries:iscsi_service')

        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message)

        return redirect('libraries:iscsi_service')


# ============================================================================
# AJAX Endpoints
# ============================================================================

class IscsiStatusAjaxView(JsonOnGet, View):
    """AJAX endpoint to get current iSCSI status."""

    def get(self, request):
        # It had no login check, unlike every other iSCSI endpoint.
        if not check_login(request):
            return _not_authenticated()
        return JsonResponse({'success': True, 'data': _status(IscsiService())})


class CreateTargetAjaxView(JsonOnGet, View):
    """AJAX endpoint to create a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()

            if not iqn:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN is required'
                })

            result = service.create_target(iqn)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class DeleteTargetAjaxView(JsonOnGet, View):
    """AJAX endpoint to delete a target.

    With ``remove_backstores`` and the library it belongs to, this unexports
    the library instead: the target and the devices it exported. Deleting the
    target alone leaves the backstores holding a /dev/sg node, which is what
    the page's checkbox is about.
    """

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()

            if not iqn:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN is required'
                })

            library_id = data.get('library_id')
            if data.get('remove_backstores') and library_id:
                result = service.unexport_library(int(library_id), iqn=iqn)
            else:
                result = service.delete_target(iqn)
            return JsonResponse(result.to_dict())

        except (json.JSONDecodeError, ValueError):
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class AddLunAjaxView(JsonOnGet, View):
    """AJAX endpoint to add a LUN to a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            backstore_plugin = data.get('backstore_plugin', '').strip()
            backstore_name = data.get('backstore_name', '').strip()
            tpg = data.get('tpg', 1)

            if not all([iqn, backstore_plugin, backstore_name]):
                return JsonResponse({
                    'success': False,
                    'error': 'IQN, backstore_plugin, and backstore_name are required'
                })

            # The LUN number the form asked for, or the next free one when it
            # is left empty. This once passed (tpg, lun_id) where the adapter
            # took (lun_id, tpg), so the TPG number went in as the LUN.
            lun_id = data.get('lun_id')
            try:
                lun_id = int(lun_id) if lun_id not in (None, '') else None
            except (TypeError, ValueError):
                return JsonResponse({'success': False,
                                     'error': f'LUN number {lun_id!r} is not a number'})
            result = service.create_lun(iqn, backstore_plugin, backstore_name, tpg=tpg,
                                        lun=lun_id)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class DeleteLunAjaxView(JsonOnGet, View):
    """AJAX endpoint to delete a LUN from a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            lun_id = data.get('lun_id')
            tpg = data.get('tpg', 1)

            if not iqn or lun_id is None:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN and lun_id are required'
                })

            result = service.delete_lun(iqn, lun_id, tpg)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class ChapAjaxView(JsonOnGet, View):
    """Set or clear CHAP on one ACL, or target-wide when no initiator is named.

    Passwords come in here and go to targetcli; they are never sent back to
    the page, which only ever shows whether CHAP is set and the user name.
    """

    def post(self, request):
        if not check_login(request):
            return _not_authenticated()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'})
        iqn = (data.get('iqn') or '').strip()
        initiator = (data.get('initiator') or '').strip() or None
        if not iqn:
            return JsonResponse({'success': False, 'error': 'IQN is required'})
        service = IscsiService()
        try:
            if data.get('action') == 'clear':
                result = service.clear_chap(iqn, initiator=initiator)
            else:
                result = service.set_chap(
                    iqn, (data.get('userid') or '').strip(), data.get('password') or '',
                    initiator=initiator,
                    mutual_userid=(data.get('mutual_userid') or '').strip(),
                    mutual_password=data.get('mutual_password') or '')
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc), 'errors': [str(exc)]})
        return JsonResponse(result.to_dict())


class AddAclAjaxView(JsonOnGet, View):
    """AJAX endpoint to add an ACL to a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            initiator_iqn = data.get('initiator_iqn', '').strip()
            tpg = data.get('tpg', 1)

            if not iqn or not initiator_iqn:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN and initiator_iqn are required'
                })

            result = service.create_acl(iqn, initiator_iqn, tpg)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class DeleteAclAjaxView(JsonOnGet, View):
    """AJAX endpoint to delete an ACL from a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            initiator_iqn = data.get('initiator_iqn', '').strip()
            tpg = data.get('tpg', 1)

            if not iqn or not initiator_iqn:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN and initiator_iqn are required'
                })

            result = service.delete_acl(iqn, initiator_iqn, tpg)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class AddPortalAjaxView(JsonOnGet, View):
    """AJAX endpoint to add a portal to a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            ip_address = data.get('ip_address', '0.0.0.0').strip()
            port = data.get('port', 3260)
            tpg = data.get('tpg', 1)

            if not iqn:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN is required'
                })

            result = service.create_portal(iqn, ip_address, port, tpg)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class DeletePortalAjaxView(JsonOnGet, View):
    """AJAX endpoint to delete a portal from a target."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            ip_address = data.get('ip_address', '').strip()
            port = data.get('port', 3260)
            tpg = data.get('tpg', 1)

            if not iqn or not ip_address:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN and ip_address are required'
                })

            result = service.delete_portal(iqn, ip_address, port, tpg)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class CreateBackstoreAjaxView(JsonOnGet, View):
    """AJAX endpoint to create a backstore."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            device_path = data.get('device_path', '').strip()
            plugin = data.get('plugin', 'pscsi')

            if not name or not device_path:
                return JsonResponse({
                    'success': False,
                    'error': 'Name and device_path are required'
                })

            if plugin == 'pscsi':
                result = service.create_backstore(device_path, name, plugin='pscsi')
            elif plugin == 'block':
                result = service.create_backstore(device_path, name, plugin='block')
            else:
                return JsonResponse({
                    'success': False,
                    'error': f'Unsupported plugin type: {plugin}'
                })

            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class DeleteBackstoreAjaxView(JsonOnGet, View):
    """AJAX endpoint to delete a backstore."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            name = data.get('name', '').strip()
            plugin = data.get('plugin', '').strip()

            if not name or not plugin:
                return JsonResponse({
                    'success': False,
                    'error': 'Name and plugin are required'
                })

            result = service.delete_backstore(plugin, name)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })


class SetGenerateNodeAclsAjaxView(JsonOnGet, View):
    """AJAX endpoint to set generate_node_acls attribute."""

    def post(self, request):
        if not check_login(request):
            return JsonResponse({
                'success': False,
                'error': 'Not authenticated'
            }, status=401)

        service = IscsiService()

        try:
            data = json.loads(request.body)
            iqn = data.get('iqn', '').strip()
            enabled = data.get('enabled', False)
            tpg = data.get('tpg', 1)

            if not iqn:
                return JsonResponse({
                    'success': False,
                    'error': 'IQN is required'
                })

            result = service.set_generate_node_acls(iqn, enabled, tpg)
            return JsonResponse(result.to_dict())

        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON'
            })
