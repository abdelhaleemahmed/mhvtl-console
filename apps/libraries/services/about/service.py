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

    WHY THE NAME IS NOT THE PACKAGE NAME
    ------------------------------------
    This said `mhvtl-gui` until 3.3.0, which is also the name of an older,
    unrelated PHP interface to MHVTL. Anyone searching for either found both,
    and a bug report quoting `mhvtl --version` did not say which program it
    was about. The product is `mhvtl-console`, and this is the one string the
    About page and `mhvtl --version` read.

    The *packaging* identity deliberately did not move with it: the RPM is
    still `mhvtl-gui`, installed at /opt/mhvtl-gui, run by the mhvtl-gui
    account under mhvtl-gui.service. Renaming those renames an upgrade path,
    a service unit, a sudoers file and a system account on every host that
    already has one, which is a migration rather than a label.
    """
    return {
        'name': 'mhvtl-console',
        'version': mhvtl_system.__version__,
        'author': mhvtl_system.__author__,
        'email': mhvtl_system.__email__,
        'licence': mhvtl_system.__licence__,
        'url': mhvtl_system.__url__,
        'docs_user': mhvtl_system.DOCS_USER,
        'docs_api': mhvtl_system.DOCS_API,
    }


def running_from() -> str:
    """Which copy of the code is answering - the installed tree, or a checkout.

    A fact about this process rather than about the project, which is why it
    is not in project() above. ``--version`` prints it anyway, because that
    is where it is needed.

    WHY IT IS PRINTED AT ALL
    ------------------------
    sudoers on a Red Hat host sets ``secure_path``, so ``sudo mhvtl`` throws
    the caller's PATH away and always resolves /usr/bin/mhvtl - and therefore
    the *installed* tree. Plain ``mhvtl`` can be pointed at a working tree
    with PATH, or with MHVTL_APP. So while a checkout is ahead of the
    package, one typed command runs two different programs depending on a
    single word:

        $ mhvtl preset list              worked
        $ sudo mhvtl preset list         invalid choice: 'preset'

    ``--version`` printed the same four lines for both, because the working
    tree and the package carried the same version string. That is how a whole
    terminal recording was made against the wrong copy on 4 October 2026
    before anybody noticed. Printing the path makes the next one visible in
    one command.

    No I/O that can fail and no Django: ``--version`` is answered at parse
    time, before setup_django has run.
    """
    from pathlib import Path

    return str(Path(mhvtl_system.__file__).resolve().parent.parent)


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
        'running_from': running_from(),
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

    Five lines rather than one, because the question behind "what version is
    this" is usually "and where do I report it" - so the address to report to
    and the licence come with it. argparse calls this once, at parse time.

    The last line is which copy of the code answered. Two copies on one host
    printed the same four lines for a day; see running_from.
    """
    it = project()
    return (f"{it['name']} {it['version']}\n"
            f"{it['author']} <{it['email']}>\n"
            f"{it['licence']}\n"
            f"{it['url']}\n"
            f"running from {running_from()}")
