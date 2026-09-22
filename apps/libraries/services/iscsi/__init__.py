"""Exporting libraries and drives over iSCSI, through targetcli.

Modules:
    models.py     the nine dataclasses that compose into IscsiStatus
    targetcli.py  the command wrapper, and the checks in front of it
    parsing.py    reading LIO's two formats: saveconfig.json and `targetcli ls`
    workflow.py   export_library: target, backstores, LUNs and the access rule
    service.py    IscsiService: status, targets, LUNs, ACLs, export

Every mutation goes through targetcli.py, which validates the IQN format and
refuses a device path that is not one of this host's MHVTL devices. Exporting a
disk that is not a virtual tape device publishes it to any initiator that
connects, and that path had no validation at all.
"""
from .models import (IscsiAcl, IscsiBackstore, IscsiLun, IscsiPortal,
                     IscsiServiceStatus, IscsiStatus, IscsiTarget, IscsiTpg)
from . import parsing
from .service import IscsiService
from .workflow import ExportReport, export_library
from .targetcli import DeviceRefused, InvalidIqn

__all__ = ['IscsiService', 'IscsiStatus', 'IscsiTarget', 'IscsiBackstore',
           'IscsiLun', 'IscsiAcl', 'IscsiPortal', 'IscsiTpg',
           'IscsiServiceStatus', 'InvalidIqn', 'DeviceRefused', 'parsing',
           'export_library', 'ExportReport']
