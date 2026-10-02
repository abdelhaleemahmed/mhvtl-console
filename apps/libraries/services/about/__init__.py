"""Who made this, which version it is, and what it is running on.

Modules:
    service.py    facts(), one_line()

A leaf, the shape dashboard/ uses: one module, in its own package because it
is its own role. It imports core and console; nothing imports it but its two
callers, the About page and `mhvtl --version`, so rule 7 in
services/__init__.py holds.

The facts themselves are in mhvtl_system/__init__.py - read its header for
why they are a constant and not distribution metadata. This package composes
and formats them, so the page and the terminal cannot disagree about what
version is running, the way the footer and the packages once did.
"""
from .service import facts, one_line, project

__all__ = ['facts', 'one_line', 'project']
