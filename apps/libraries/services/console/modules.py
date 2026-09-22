"""Kernel module state.

Moved from console_service.py:382 (get_kernel_modules) and :402
(check_mhvtl_modules_loaded).

Two things it has to get right. A module can be built in rather than loadable,
so `lsmod` alone reports a working system as broken; and MHVTL 1.8 can run on
the TCMU backend instead of mhvtl.ko, in which case target_core_user and tcm_loop
are the modules that matter and mhvtl.ko is legitimately absent.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Dict, List, Set

from ..core import QUICK, shell

logger = logging.getLogger(__name__)

REQUIRED = ('mhvtl',)
OPTIONAL = ('sg',)
TCMU = ('target_core_user', 'tcm_loop')
ISCSI = ('target_core_mod', 'iscsi_target_mod', 'target_core_pscsi')


def _loaded_names() -> Set[str]:
    result = shell.run(['lsmod'], timeout=QUICK)
    if not result.ok:
        return set()
    return {line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()}


def _builtin_names() -> Set[str]:
    """Modules compiled into the kernel, which lsmod never lists."""
    release = shell.run(['uname', '-r'], timeout=QUICK)
    if not release.ok:
        return set()
    path = f'/lib/modules/{release.stdout.strip()}/modules.builtin'
    result = shell.run(['cat', path], timeout=QUICK)
    if not result.ok:
        return set()
    return {line.rsplit('/', 1)[-1].replace('.ko', '').strip()
            for line in result.stdout.splitlines() if line.strip()}


def loaded() -> Dict[str, Dict]:
    """Every module this application cares about, and whether it is available."""
    available = _loaded_names() | _builtin_names()

    def describe(names, required, category):
        return {name: {'loaded': name in available, 'required': required,
                       'category': category} for name in names}

    modules = {}
    modules.update(describe(REQUIRED, True, 'MHVTL'))
    modules.update(describe(OPTIONAL, False, 'MHVTL'))
    modules.update(describe(TCMU, False, 'MHVTL (TCMU backend)'))
    modules.update(describe(ISCSI, False, 'iSCSI (LIO)'))
    return modules


def mhvtl_loaded() -> bool:
    """Is a usable MHVTL backend present - the kernel module or TCMU?

    1.8 can run either way, so insisting on mhvtl.ko would report a working
    TCMU installation as broken.
    """
    available = _loaded_names() | _builtin_names()
    return 'mhvtl' in available or all(name in available for name in TCMU)


def summary() -> Dict:
    state = loaded()
    return {
        'modules': state,
        'all_required_loaded': all(info['loaded'] for info in state.values()
                                   if info['required']),
        'backend': 'mhvtl' if state.get('mhvtl', {}).get('loaded')
                   else ('tcmu' if all(state.get(n, {}).get('loaded') for n in TCMU)
                         else 'none'),
    }
