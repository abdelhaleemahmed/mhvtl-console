"""Tape drive lifecycle. This role had no module before the refactor.

Modules:
    models.py    DriveInfo
    service.py   DriveService: list, get, add, remove, update

The implementations were stranded in backup_mhvtl_library_service.py, a fork with
zero importers, while the views called them on a service that did not define
them - so drive add and remove have never worked in the web UI.
"""
from .models import DriveInfo
from .service import DriveService

__all__ = ['DriveService', 'DriveInfo']
