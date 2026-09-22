"""Proving a library holds what is written to it.

Modules:
    models.py    StepResult and VerificationReport - what a run reports
    data.py      generating test data, checksumming it, checking it came back
    tape_io.py   writing an archive to a tape device and reading it back
    workflow.py  the eight-step run, in order
    service.py   VerificationService: the facade a view or the CLI holds

Moved from services/backup_restore_test_service.py, which did all five jobs in
one file and called subprocess directly for tar and mt.

The step that makes it a verification rather than a file copy is the unmount
between writing and reading: it forces the drive to flush and the robot to put
the cartridge away, so the read comes off the medium rather than out of a buffer
that still holds what was just written.
"""
from .models import StepResult, VerificationReport
from .service import VerificationService

__all__ = ['VerificationService', 'VerificationReport', 'StepResult']
