"""Creating a library and making it real.

Extracted from views.py:432-520, where the whole sequence lived inside a POST
handler interleaved with `messages.success(...)` calls. That is why there has
never been a CLI `library create`: the steps after "write the config" - restart
the daemons, check MHVTL actually picked it up, create the media files - existed
only as view code.

The sequence, and why it is a sequence:

    1. validate      a bad specification should cost nothing
    2. create        write device.conf and library_contents
    3. restart       the daemons read their configuration at startup, so a new
                     library does not exist until they are restarted
    4. verify        ask the running system whether it can see the library;
                     a config file that parses is not the same as a working
                     library
    5. media         create the tape files for the barcodes library_contents
                     now declares. generate_library_contents writes the slot
                     list; it does not create the media

Steps 3 to 5 are reported but not fatal: a library whose config is written and
whose daemons did not restart is a library that needs a restart, not a failed
creation. The caller gets every step back and decides what to show.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core import ServiceResult, failure_result, success_result
from .service import LibraryService

logger = logging.getLogger(__name__)

DEFAULT_TAPE_SIZE_MB = 500


@dataclass
class Step:
    """One stage of the workflow, and how it went."""
    name: str
    ok: bool
    message: str
    fatal: bool = False

    def to_dict(self) -> Dict:
        return {'step': self.name, 'ok': self.ok, 'message': self.message,
                'fatal': self.fatal}


@dataclass
class WorkflowReport:
    """Every step, in order, for a caller to render however it likes."""
    steps: List[Step] = field(default_factory=list)
    library_id: Optional[int] = None

    def add(self, name: str, ok: bool, message: str, *, fatal: bool = False) -> Step:
        step = Step(name=name, ok=ok, message=message, fatal=fatal)
        self.steps.append(step)
        logger.info('create-library %s: %s - %s', name,
                    'ok' if ok else 'failed', message)
        return step

    @property
    def failed(self) -> bool:
        return any(step.fatal for step in self.steps)

    @property
    def warnings(self) -> List[str]:
        return [step.message for step in self.steps if not step.ok and not step.fatal]

    def to_dict(self) -> Dict:
        return {'library_id': self.library_id,
                'steps': [step.to_dict() for step in self.steps],
                'warnings': self.warnings}


def create_library_workflow(spec: Dict, *, restart: bool = True,
                            create_media: bool = True,
                            config_directory=None) -> ServiceResult:
    """Create a library, restart the daemons, verify it, and make its media.

    Usage::

        result = create_library_workflow({'profile': 'STK', 'library_id': 40, ...})
        for step in result.data['steps']:
            print(step['step'], step['message'])

    Returns a failure only when the library itself could not be created; the
    later steps are reported as warnings, because a written configuration is not
    undone by a daemon that did not come back.
    """
    operation_id = str(uuid.uuid4())[:8]
    service = LibraryService(config_directory)
    report = WorkflowReport()

    # 1. validate
    validation = service.validate(spec)
    if not validation.success:
        report.add('validate', False, validation.message, fatal=True)
        rejected = failure_result(
            f'Library specification rejected: {validation.message}',
            validation.errors, operation_id)
        # Callers render the steps; a caller that only gets a message cannot say
        # which stage refused.
        rejected.data = report.to_dict()
        return rejected
    report.add('validate', True, 'Specification is valid')
    for warning in (validation.data or {}).get('warnings', []):
        report.add('validate', False, warning)

    # 2. create - the daemons are started here only when they are to be
    # restarted below; restart=False leaves them down
    created = service.create(spec, start_services=restart)
    if not created.success:
        report.add('create', False, created.message, fatal=True)
        result = failure_result(created.message, created.errors, operation_id)
        result.data = report.to_dict()
        return result

    library_id = (created.data or {}).get('library_id') or spec.get('library_id')
    report.library_id = library_id
    report.add('create', True, created.message)

    # 3. restart - a new library does not exist until the daemons reread the config
    if restart:
        restarted = service.restart_services(library_id)
        report.add('restart', restarted.success,
                   restarted.message if restarted.success
                   else f'Library created, but the services did not restart: '
                        f'{restarted.message}')

        # 4. verify - only meaningful once the daemons have restarted
        if service.recognised_by_mhvtl(library_id):
            report.add('verify', True, f'MHVTL can see library {library_id}')
        else:
            report.add('verify', False,
                       f'Library {library_id} is configured but MHVTL does not '
                       f'report it yet; it may still be starting')

    # 5. media - library_contents lists the barcodes, but the files are not there yet
    if create_media:
        report.add(*_create_media(library_id, spec, config_directory))

    return success_result(
        f'Library {library_id} created'
        + (f' with {len(report.warnings)} warning(s)' if report.warnings else ''),
        {**report.to_dict(), **(created.data or {})}, operation_id)


def _create_media(library_id: int, spec: Dict, config_directory=None):
    """Create the tape files for the barcodes library_contents now declares.

    Called services/tapes directly; it used to go through the old adapter,
    whose method did not take the density passed here, so every library
    created since the cutover got its configuration and no tapes.
    """
    from ..tapes import TapeService

    try:
        result = TapeService(config_directory).create_missing(
            library_id,
            size_mb=spec.get('tape_size_mb', DEFAULT_TAPE_SIZE_MB),
            density=spec.get('media_type') or spec.get('density'))
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('creating media for library %s', library_id)
        return ('media', False, f'Library created, but its tape media was not: {exc}')
    if result.success:
        return ('media', True, result.message)
    return ('media', False,
            f'Library created, but not all of its tape media was: {result.message}')
