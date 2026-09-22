"""Library lifecycle: list, create, update, delete, orphans.

Modules:
    models.py     LibraryInfo
    service.py    LibraryService: list, get, next_id, create, update, delete
    spec.py       filling a specification in from its vendor profile
    validation.py the vendor-profile rules applied to a finished specification
    lifecycle.py  create and delete: the paths that write device.conf, with the
                  backup and rollback around them
    workflow.py   create_library_workflow - the multi-step creation that used to
                  live inside a view, and the reason there was no CLI equivalent
    orphans.py    libraries the database and device.conf disagree about

Creating a library reads as its five steps in lifecycle.create: apply the
profile defaults, validate, back up, write device.conf, write the contents file.
Update, delete and preview live in lifecycle too; workflow.py adds the restart,
the check that MHVTL sees the library, and the tape files.
"""
from . import lifecycle, spec, validation
from .models import LibraryInfo
from .service import LibraryService
from .workflow import WorkflowReport, create_library_workflow

__all__ = ['LibraryService', 'LibraryInfo', 'create_library_workflow',
           'WorkflowReport', 'validation', 'spec', 'lifecycle']
