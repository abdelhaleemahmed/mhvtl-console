"""The result type every service returns.

Moved from services/service_results.py, which defined exactly this contract and
was imported by nothing while four rival ServiceResult classes grew elsewhere
(mhvtl_library_service.py:76, backup_mhvtl_library_service.py:33,
iscsi_service.py:182). Those collapse onto this one as each domain moves.

Why it matters for the CLI: every result carries .to_dict(), so a command can
print JSON without per-command formatting code, and a caller can tell success
from failure without knowing which service it called.

Two types only: ServiceResult for anything that acts, ValidationResult for
anything that checks. The module this moved from also defined StatusResult,
OperationMetrics, LibraryInfo, OperationType and a LibraryStatus constants
class, none of which had a single caller - and two of those names collided with
live classes elsewhere, operations/mtx.LibraryStatus and
libraries/models.LibraryInfo. They went at step 8; a domain model does not
belong in the layer everything else imports.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime


@dataclass
class ServiceResult:
    """
    Standard result format for service operations
    
    Used for all MHVTL service operations that modify system state
    (create, update, delete, regenerate, etc.)
    """
    success: bool
    message: str
    data: Optional[Dict] = None
    errors: List[str] = field(default_factory=list)
    operation_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    
    def add_error(self, error: str) -> None:
        """Add an error to the errors list"""
        self.errors.append(error)
        self.success = False
    
    def add_data(self, key: str, value: Any) -> None:
        """Add data to the result"""
        if self.data is None:
            self.data = {}
        self.data[key] = value
    
    def to_dict(self) -> Dict:
        """Convert result to dictionary for JSON serialization"""
        return {
            'success': self.success,
            'message': self.message,
            'data': self.data,
            'errors': self.errors,
            'operation_id': self.operation_id,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class ValidationResult:
    """
    Result format for validation operations
    
    Used for validating library data before performing operations
    """
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggested_fixes: List[str] = field(default_factory=list)
    
    def add_error(self, error: str) -> None:
        """Add a validation error"""
        self.errors.append(error)
        self.is_valid = False
    
    def add_warning(self, warning: str) -> None:
        """Add a validation warning"""
        self.warnings.append(warning)
    
    def add_suggested_fix(self, fix: str) -> None:
        """Add a suggested fix"""
        self.suggested_fixes.append(fix)
    
    def to_dict(self) -> Dict:
        """Convert result to dictionary"""
        return {
            'is_valid': self.is_valid,
            'errors': self.errors,
            'warnings': self.warnings,
            'suggested_fixes': self.suggested_fixes
        }


def success_result(message: str, data: Optional[Dict] = None, operation_id: Optional[str] = None) -> ServiceResult:
    """Create a successful ServiceResult"""
    return ServiceResult(
        success=True,
        message=message,
        data=data,
        operation_id=operation_id
    )


def failure_result(message: str, errors: Optional[List[str]] = None, operation_id: Optional[str] = None) -> ServiceResult:
    """Create a failed ServiceResult"""
    return ServiceResult(
        success=False,
        message=message,
        errors=errors or [],
        operation_id=operation_id
    )
