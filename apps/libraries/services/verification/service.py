"""The facade: run a verification, and report it as a ServiceResult.

Everything here is thin. The steps are in workflow.py, the data handling in
data.py and the tape I/O in tape_io.py; this exists so a caller - a view, or
the CLI - has one object to hold and one result shape to read.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid

from ..core import ServiceResult, config_dir, failure_result, success_result
from . import workflow
from .models import VerificationReport

logger = logging.getLogger(__name__)


class VerificationService:
    """End-to-end proof that a library holds what is written to it.

        VerificationService().run(library_id=10, slot=1, drive_id=11)
    """

    def __init__(self, config_directory=None, work_dir=None):
        self.config_dir = config_directory or config_dir()
        self.work_dir = work_dir

    def run(self, library_id: int, slot: int, drive_id: int, *,
            size_mb: int = 10, file_count: int = 5,
            cleanup_on_success: bool = True) -> ServiceResult:
        """Write data to a tape, read it back, and check it is the same.

        The tape in `slot` is overwritten. That is what the operation is, but it
        is worth the caller saying so to whoever presses the button.
        """
        operation_id = str(uuid.uuid4())[:8]
        report = workflow.run(library_id, slot, drive_id, size_mb=size_mb,
                              file_count=file_count, work_dir=self.work_dir,
                              cleanup_on_success=cleanup_on_success,
                              config_dir=self.config_dir)

        if report.success:
            return success_result(
                f'Library {library_id} verified: {size_mb}MB written to slot '
                f'{slot} and read back identical in '
                f'{report.duration_seconds:.0f}s',
                report.to_dict(), operation_id)

        failed = report.failed_step
        return failure_result(
            f'Verification failed at "{failed.step}": {failed.message}'
            if failed else 'Verification failed',
            (failed.errors if failed and failed.errors else [])
            + [f'the working directory has been kept: '
               f'{report.summary.get("work_dir") if report.summary else "unknown"}'],
            operation_id)

    def report(self, library_id: int, slot: int, drive_id: int,
               **kwargs) -> VerificationReport:
        """The full step-by-step report, for a caller that wants every step."""
        return workflow.run(library_id, slot, drive_id,
                            work_dir=self.work_dir, config_dir=self.config_dir,
                            **kwargs)
