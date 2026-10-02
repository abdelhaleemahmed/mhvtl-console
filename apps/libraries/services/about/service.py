"""The console's identity, composed for whoever is asking.

Two callers, one answer: the About page renders this as HTML and
`mhvtl --version` prints it as text. Neither knows anything the other does
not, which is the same rule the LTO palette follows - decided once in
services/tapes/palette.py, rendered by the stylesheet and by the terminal.

WHAT IS A FACT ABOUT THE PROJECT, AND WHAT IS A FACT ABOUT THIS HOST
--------------------------------------------------------------------
project() answers the first and cannot fail: five strings that ship with the
code. runtime() answers the second and composes services already written -
console/units for whether mhvtl.target is up, console/modules for the backend
- rather than reading systemd a second time. A bug report needs both halves,
and the second is the half that goes stale.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell - by way of console/, here
    - only sync/ may import apps.libraries.models
"""
import logging
import platform
from typing import Any, Dict, Optional

import mhvtl_system

from ..core import ServiceResult, success_result

logger = logging.getLogger(__name__)


def project() -> Dict[str, str]:
    """What this is, and who is answerable for it.

    Five strings from mhvtl_system, plus the two documentation addresses.
    No I/O, nothing that can fail - a caller that only wants the version
    does not have to handle a failure that cannot happen.
    """
    return {
        'name': 'mhvtl-gui',
        'version': mhvtl_system.__version__,
        'author': mhvtl_system.__author__,
        'email': mhvtl_system.__email__,
        'licence': mhvtl_system.__licence__,
        'url': mhvtl_system.__url__,
        'docs_user': mhvtl_system.DOCS_USER,
        'docs_api': mhvtl_system.DOCS_API,
    }


def runtime(config_directory: Optional[str] = None) -> Dict[str, Any]:
    """What it is running on, for a bug report.

    Each part is taken separately and a failure is recorded rather than
    raised: a wedged systemctl should cost the MHVTL line on the About page,
    not the version above it. The pattern is dashboard/service.py's.
    """
    import django

    runtime: Dict[str, Any] = {
        'python': platform.python_version(),
        'django': django.get_version(),
        'platform': platform.platform(),
        'hostname': platform.node(),
    }

    try:
        from ..console import units
        state = units.status(config_directory)
        runtime['mhvtl_target'] = 'running' if state.target_active else 'stopped'
        runtime['mhvtl_libraries'] = f'{state.libraries_active}/{len(state.libraries)}'
        runtime['mhvtl_drives'] = f'{state.drives_active}/{len(state.drives)}'
    except Exception as error:                      # noqa: BLE001 - reported, not raised
        logger.warning('about: reading the MHVTL units failed: %s', error)
        runtime['mhvtl_error'] = str(error)

    try:
        from ..console import modules
        runtime['backend'] = modules.summary()['backend']
    except Exception as error:                      # noqa: BLE001
        logger.warning('about: reading the kernel modules failed: %s', error)
        runtime['backend_error'] = str(error)

    return runtime


def facts(config_directory: Optional[str] = None) -> ServiceResult:
    """Everything the About page shows: the project, and this host."""
    data = {'project': project(), 'runtime': runtime(config_directory)}
    return success_result(
        f"{data['project']['name']} {data['project']['version']}", data)


def one_line() -> str:
    """What `mhvtl --version` prints.

    Four lines rather than one, because the question behind "what version is
    this" is usually "and where do I report it" - so the address to report to
    and the licence come with it. argparse calls this once, at parse time.
    """
    it = project()
    return (f"{it['name']} {it['version']}\n"
            f"{it['author']} <{it['email']}>\n"
            f"{it['licence']}\n"
            f"{it['url']}")
