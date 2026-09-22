"""Configuration files to database - the only package here that touches the ORM.

Modules:
    service.py    sync_mhvtl_to_django()

The boundary is deliberate: the config files are authoritative and the database
is a cache of them. Everything else in services stays ORM-free so a CLI can
call it without Django's app registry.
"""
from .service import sync_mhvtl_to_django

__all__ = ['sync_mhvtl_to_django']
