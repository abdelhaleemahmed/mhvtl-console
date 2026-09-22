"""Overview data for the dashboard tiles.

Moved from services/dashboard_service.py, unchanged: it was written during this
refactor and is already in the target shape.

Its _section() helper is the failure-isolation pattern the rest of the layer
adopted - each collector runs separately, a failure is reported rather than
raised, and one wedged systemctl degrades one tile instead of blanking the page.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Section:
    """One part of the summary, which may have failed on its own."""
    ok: bool = True
    error: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


def _section(fn) -> Section:
    """Run a collector, turning any failure into a reportable section."""
    try:
        return Section(ok=True, data=fn())
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        logger.warning('dashboard section %s failed: %s', getattr(fn, '__name__', fn), exc)
        return Section(ok=False, error=str(exc))


def _service_status() -> Dict[str, Any]:
    from apps.libraries.services.console import modules, units

    status = units.status()
    summary = modules.summary()
    mhvtl_module = summary.get('modules', {}).get('mhvtl', {})

    return {
        'running': status.target_active,
        'enabled': status.target_enabled,
        'state': 'active' if status.target_active else 'inactive',
        'healthy': status.healthy,
        'module_loaded': bool(mhvtl_module.get('loaded')),
        'all_required_modules': bool(summary.get('all_required_loaded')),
        'daemons_active': status.daemons_active,
        'daemons_total': status.daemons_total,
        'libraries_active': status.libraries_active,
        'libraries_total': len(status.libraries),
        'drives_active': status.drives_active,
        'drives_total': len(status.drives),
    }


def _library_summary() -> Dict[str, Any]:
    """What device.conf declares, through the library service.

    Read through services/libraries rather than the adapter, and so through
    ConfigService, which uses sudo when the file is root-owned. A default MHVTL
    install ships device.conf as mode 0600, so anything reading it with a plain
    open() reports a host with no libraries at all.
    """
    from apps.libraries.services.config.service import ConfigService
    from apps.libraries.services.libraries import LibraryService

    config = ConfigService()
    result = LibraryService(config.config_dir).list()
    if not result.success:
        raise RuntimeError('; '.join(result.errors) or result.message)

    libraries = [
        {'library_id': entry['library_id'], 'vendor': entry['vendor'],
         'product': entry['product'], 'serial': entry['serial'],
         'status': 'active' if entry['slot_count'] is not None
                   else 'missing_contents',
         'drives': entry['drives']}
        for entry in result.data['libraries']]

    return {
        'libraries': libraries,
        'count': len(libraries),
        'total_drives': sum(entry['drives'] for entry in libraries),
    }


def _media_summary() -> Dict[str, Any]:
    """Tape counts per library, read from library_contents rather than mtx.

    mtx would mean one SCSI round trip per library and blocks while a drive is
    busy; the config files give the same inventory for free.

    Read through ConfigService, which falls back to sudo on a file this process
    cannot open. The version this replaces globbed the directory and called
    read_text() with its own slot regex - a fifth copy of the library_contents
    parser - and on a default install, where the directory is not world
    readable, it reported every library as unreadable.
    """
    from apps.libraries.services.config.service import ConfigService

    config = ConfigService()
    per_library: Dict[int, int] = {}
    unreadable: List[str] = []
    cleaning = 0
    total = 0

    for entry in config.files():
        name = getattr(entry, 'name', str(entry))
        suffix = name.rsplit('.', 1)[-1]
        if not name.startswith('library_contents.') or not suffix.isdigit():
            continue

        library_id = int(suffix)
        contents = config.library_contents(library_id)
        if contents is None:
            # Do not quietly drop the library: one that cannot be read would
            # otherwise look like a library with no tapes in it.
            logger.warning('cannot read %s', name)
            unreadable.append(name)
            continue

        occupied = contents.occupied
        per_library[library_id] = len(occupied)
        total += len(occupied)
        cleaning += sum(1 for slot in occupied if slot.kind == 'clean')

    # No config files at all means we have nothing to count, which is not the
    # same as counting zero tapes - say so rather than report a confident 0.
    found_any = bool(per_library) or bool(unreadable)

    return {
        'total': total if found_any else None,
        'cleaning': cleaning,
        'data_tapes': (total - cleaning) if found_any else None,
        'per_library': per_library,
        'unreadable': unreadable,
        'partial': bool(unreadable),
        'known': found_any,
        'config_dir': str(config.config_dir),
    }


def _storage_summary() -> Dict[str, Any]:
    from apps.libraries.services.console import disk

    usage = disk.media_usage()
    return {
        'data_size': usage['size'] or 'Unknown',
        'tape_count': usage['tape_count'],
        'path': usage['path'],
    }


def get_dashboard_summary() -> Dict[str, Any]:
    """Everything the overview tiles need, in one call."""
    sections = {
        'service': _section(_service_status),
        'libraries': _section(_library_summary),
        'media': _section(_media_summary),
        'storage': _section(_storage_summary),
    }

    summary = {name: asdict(section) for name, section in sections.items()}
    summary['alerts'] = _alerts(sections)
    summary['degraded'] = [name for name, section in sections.items() if not section.ok]
    return summary


def _alerts(sections: Dict[str, Section]) -> List[Dict[str, str]]:
    """Things worth saying out loud at the top of the page."""
    alerts: List[Dict[str, str]] = []

    service = sections['service']
    if service.ok:
        if not service.data.get('module_loaded'):
            alerts.append({
                'level': 'danger',
                'message': 'The mhvtl kernel module is not loaded, so no virtual '
                           'devices exist. Load it with "modprobe mhvtl".',
            })
        if not service.data.get('running'):
            alerts.append({
                'level': 'danger',
                'message': 'The mhvtl daemons are not running. Start them with '
                           '"systemctl start mhvtl.target".',
            })
    else:
        alerts.append({
            'level': 'warning',
            'message': f'Could not read the mhvtl service state: {service.error}',
        })

    media = sections['media']
    if media.ok and not media.data.get('known'):
        alerts.append({
            'level': 'warning',
            'message': f"No library_contents files found in {media.data.get('config_dir')}, "
                       f'so tape counts are unavailable.',
        })
    if media.ok and media.data.get('unreadable'):
        files = ', '.join(media.data['unreadable'])
        alerts.append({
            'level': 'warning',
            'message': f'Tape counts are incomplete: cannot read {files}. '
                       f'Check that the service user can read /etc/mhvtl.',
        })

    libraries = sections['libraries']
    if libraries.ok and libraries.data.get('count') == 0:
        alerts.append({
            'level': 'warning',
            'message': 'No libraries are configured yet.',
        })
    elif not libraries.ok:
        alerts.append({
            'level': 'warning',
            'message': f'Could not read the library configuration: {libraries.error}',
        })

    return alerts
