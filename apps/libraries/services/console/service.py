"""The facade the console pages call.

Moved from console_service.py:684 (get_console_summary). The work is in the
modules beside this one; this assembles their answers.

Each section is gathered separately and a failure is reported rather than
raised, the pattern dashboard_service established: one wedged systemctl degrades
one panel instead of blanking the page.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from typing import Any, Callable, Dict

from ..core import ServiceResult, success_result
from . import disk, logs, modules, system, units

logger = logging.getLogger(__name__)


def _section(name: str, collect: Callable[[], Any]) -> Dict:
    try:
        return {'ok': True, 'error': None, 'data': collect()}
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.warning('console section %s failed: %s', name, exc)
        return {'ok': False, 'error': str(exc), 'data': None}


class ConsoleService:
    """Everything the console pages show.

        ConsoleService().summary()
        ConsoleService().service_status()
    """

    def __init__(self, config_directory=None):
        self.config_dir = config_directory

    def service_status(self) -> units.MhvtlServiceStatus:
        """The MHVTL unit hierarchy.

        This is the get_service_status() that views.py:181 and
        iscsi_views.py:446 have been calling on a service that never defined it.
        """
        return units.status(self.config_dir)

    def summary(self) -> ServiceResult:
        """One answer for the whole console page."""
        operation_id = str(uuid.uuid4())[:8]
        sections = {
            'system': _section('system', lambda: system.info().to_dict()),
            'services': _section('services', lambda: self.service_status().to_dict()),
            'modules': _section('modules', modules.summary),
            'disk': _section('disk', lambda: [d.to_dict() for d in disk.usage()]),
            'media': _section('media', disk.media_usage),
            'logs': _section('logs', lambda: {'available': logs.available()}),
        }
        degraded = [name for name, section in sections.items() if not section['ok']]

        return success_result(
            'Console summary' + (f' ({len(degraded)} section(s) unavailable)'
                                 if degraded else ''),
            {**sections, 'degraded': degraded}, operation_id)
