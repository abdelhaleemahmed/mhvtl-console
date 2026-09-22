"""What a verification run reports.

Moved from backup_restore_test_service.py (TestResult, FullTestResult).

A verification is a sequence of steps, and the interesting output is not a
boolean but which step failed and how long each took. A restore that verifies
in 40 seconds and one that verifies in 20 minutes are both successes and only
one of them means the library is usable.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StepResult:
    """One step of a verification run."""
    step: str
    success: bool
    message: str
    duration_seconds: float = 0.0
    data: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {'step': self.step, 'success': self.success,
                'message': self.message,
                'duration_seconds': round(self.duration_seconds, 2),
                'data': self.data, 'errors': self.errors}


@dataclass
class VerificationReport:
    """A whole run: every step, in order, with what it cost."""
    run_id: str
    started_at: str
    finished_at: str = ''
    steps: List[StepResult] = field(default_factory=list)
    summary: Optional[Dict[str, Any]] = None

    @property
    def success(self) -> bool:
        """Every step passed. An empty run is not a pass."""
        return bool(self.steps) and all(step.success for step in self.steps)

    @property
    def duration_seconds(self) -> float:
        return sum(step.duration_seconds for step in self.steps)

    @property
    def failed_step(self) -> Optional[StepResult]:
        """The first step that failed, which is the one worth reporting.

        Later steps fail because this one did - a restore cannot verify data
        that was never written - so naming them all buries the cause.
        """
        return next((step for step in self.steps if not step.success), None)

    def add(self, step: StepResult) -> StepResult:
        self.steps.append(step)
        return step

    def to_dict(self) -> Dict:
        failed = self.failed_step
        return {
            'run_id': self.run_id,
            'success': self.success,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'total_duration_seconds': round(self.duration_seconds, 2),
            'steps': [step.to_dict() for step in self.steps],
            'failed_step': failed.step if failed else None,
            'summary': self.summary,
        }
