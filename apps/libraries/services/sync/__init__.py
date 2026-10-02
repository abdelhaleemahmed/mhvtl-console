"""Configuration files to database - the only package here that touches the ORM.

Modules:
    service.py    record_library(), forget_library(), sync_mhvtl_to_django()

The boundary is deliberate: the config files are authoritative and the database
is a cache of them. Everything else in services stays ORM-free so a CLI can
call it without Django's app registry.

Two scopes, and the difference matters:

    record_library(id) / forget_library(id)
        one library's row and its drives. What an operation that changed one
        library calls, so creating library 60 cannot touch library 20.
    sync_mhvtl_to_django()
        the whole configuration against the whole database, deactivating rows
        device.conf no longer mentions. An explicit repair, not a step in
        another operation.
"""
from .service import forget_library, record_library, sync_mhvtl_to_django

__all__ = ['forget_library', 'record_library', 'sync_mhvtl_to_django']
