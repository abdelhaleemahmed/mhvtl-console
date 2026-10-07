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

from ..core import (ServiceResult, config_dir, failure_result,
                    success_result)
from .service import LibraryService

logger = logging.getLogger(__name__)

#: No tape size here any more. It was 500 MB, while every front end offered
#: 500000, so a library's own cartridges were a thousandth of the size of any
#: added to it afterwards. `tape_size_mb` in the specification still overrides
#: it; with nothing given, services/tapes gives each cartridge its density's
#: native capacity (tapes.service.native_size_mb).


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
        report.add(*_restart_step(library_id, config_directory, service,
                                  what='Library created'))

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

    # 6. record - the web UI is a database reader, so a library it cannot see in
    # the database does not exist for it. This used to live in the view that
    # called this workflow (views.py:673), which is why a library created from
    # the CLI was configured, running, and invisible in every dropdown.
    report.add(*_record(library_id, config_directory))

    return success_result(
        f'Library {library_id} created'
        + (f' with {len(report.warnings)} warning(s)' if report.warnings else ''),
        {**report.to_dict(), **(created.data or {})}, operation_id)


def _restart_step(library_id: int, config_directory, service, *, what: str,
                  start_drive: int = None):
    """Restart this library's daemons - but only if they read this directory.

    THE GUARD IS THE POINT. The daemons read the live configuration directory.
    A workflow pointed somewhere else - a test, a scratch copy, a restore being
    staged - must not restart them: it would apply a file they never read and
    interrupt libraries that are in use. lifecycle.daemons_are_ours() says so
    in as many words, and orphans.cleanup() has honoured it since it was
    written.

    This function exists because the media workflow did not. Run against a copy
    of device.conf on 1 October 2026 it restarted the live vtllibrary@60, which
    unloaded the cartridge from drive 0 while an `ltfs` process still held the
    device - a mount pointing at a drive whose medium had gone. Nothing was
    lost, and it was exactly the harm the guard was written to prevent.

    `start_drive` is a NEW drive's id. Restarting the library restarts the
    robot and nothing else - vtltape@<id> is a unit of its own, and a drive
    whose daemon was never started has no device node at all. It is in
    device.conf, `ltfs drives` reports that LTFS can open it, and it cannot be
    used for anything: there is nothing to open.

    Found by recording the LTFS video (tests/e2e/video-ltfs): `ltfs add-drive`
    reported six green steps and left a drive that `lsscsi` had never heard
    of. DriveService.add() has always started the unit - units.start_library()
    with the one drive - and this is the same call, so the two paths now agree.
    """
    from ..console import units
    from .lifecycle import daemons_are_ours

    if not daemons_are_ours(config_directory or config_dir()):
        return ('restart', True,
                f'{what} written; the daemons were NOT restarted because '
                f'{config_directory} is not the live configuration directory')

    if start_drive is not None:
        started = units.start_library(int(library_id), [int(start_drive)])
        failed = [unit for unit, ok in started.items() if not ok]
        if failed:
            return ('restart', False,
                    f'{what} written, but {", ".join(failed)} did not start; '
                    f'the drive has no device until it does')

    restarted = service.restart_services(library_id)
    return ('restart', restarted.success,
            restarted.message if restarted.success
            else f'{what} written, but the services did not restart: '
                 f'{restarted.message}')


def _record(library_id: int, config_directory=None):
    """Write this one library into the database, and report it as a step.

    Scoped on purpose - see sync.record_library. A failure here is a warning,
    not fatal: the library is configured and running, and the database is a
    cache that can be rebuilt.
    """
    from ..sync import record_library

    try:
        recorded = record_library(library_id, config_directory)
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('recording library %s in the database', library_id)
        return ('record', False,
                f'Library created, but the database was not updated: {exc}')
    return ('record', recorded['ok'], recorded['message'])


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
            size_mb=spec.get('tape_size_mb'),
            # One size per kind of cartridge, from the wizard's rows. A
            # library can hold LTO-8 and DLT-4 and those are not the same
            # size; a density the wizard did not name falls through to the
            # settings file.
            sizes=spec.get('tape_sizes'),
            density=spec.get('media_type') or spec.get('density'))
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('creating media for library %s', library_id)
        return ('media', False, f'Library created, but its tape media was not: {exc}')
    if result.success:
        return ('media', True, result.message)
    return ('media', False,
            f'Library created, but not all of its tape media was: {result.message}')


# -- making a library LTFS-capable ------------------------------------------
#
# A library whose drives LTFS refuses has, until now, had to be deleted and
# rebuilt with a different profile - which throws away its cartridges. Library
# 50 is the case: two LTO-8 drives that LTFS will not open, because the STK
# profile labels them vendor `STK` and LTFS has no STK table at all
# (vendor_compat.c:314). Adding one drive it *will* open fixes that in place.
#
# This workflow ADDS. It never removes. If there is no room for another drive it
# says what is full and stops: removing a drive restarts a library and takes a
# drive away from whatever might be using it, which is an operator's decision
# and not a side effect of wanting LTFS.
#
# It also stops short of formatting. mkltfs partitions and erases a cartridge,
# it is off twice over on purpose (a setting and a withheld sudoers rule), and
# it stays a separate deliberate act. This gets as far as "a drive LTFS can
# open, and cartridges in slots".

def add_ltfs_drive_workflow(library_id: int, *, model: str = None,
                            vendor: str = None, revision: str = None,
                            tapes: int = 0, expand_slots: bool = False,
                            config_directory=None) -> ServiceResult:
    """Add a drive LTFS will open to an existing library, and optionally tapes.

    Usage::

        result = add_ltfs_drive_workflow(50, vendor='HPE',
                                         model='Ultrium 8-SCSI', tapes=2)
        for step in result.data['steps']:
            print(step['step'], step['message'])

    model and vendor default to the library's own drive vendor and the first
    model of it LTFS accepts, so the common case needs neither. revision is only
    consulted for the three IBM families that have a firmware minimum; HP, HPE
    and Quantum drives have none (see profiles/ltfs_support.py).

    tapes creates cartridges for the new drive. expand_slots must be asked for:
    a library's slot count is part of what it advertises to backup software, and
    growing it because somebody wanted two cartridges is not ours to decide.
    """
    from ..config.service import ConfigService
    from ..profiles import ltfs_support
    from . import lifecycle

    operation_id = str(uuid.uuid4())[:8]
    library_id = int(library_id)
    report = WorkflowReport()
    report.library_id = library_id
    service = LibraryService(config_directory)

    def refuse(message, errors):
        result = failure_result(message, errors, operation_id)
        result.data = report.to_dict()
        return result

    # 1. validate - the model must be one LTFS opens, decided before anything
    # is written. Adding a drive LTFS still refuses would be worse than
    # refusing to add one.
    conf = ConfigService(config_directory).device_conf()
    declared = conf.libraries.get(library_id) if conf else None
    if declared is None:
        report.add('validate', False, f'Library {library_id} is not in device.conf',
                   fatal=True)
        return refuse(f'Library {library_id} is not in device.conf',
                      ['a drive can only be added to a library that exists'])

    # The drive has to satisfy three things at once, and they are not
    # independent: LTFS must know the VENDOR id, MHVTL must emulate the MODEL,
    # and the library's own profile must take that model. Choosing them
    # separately does not work - and library 50 is why.
    #
    # An STK SL500's profile takes `ULT3580-TD8`, because a StorageTek library
    # really does hold drives with IBM part numbers, and MHVTL emulates that
    # model. What LTFS refuses is the vendor id `STK`. Ask for an HPE drive
    # instead and the profile refuses `Ultrium 8-SCSI`, which no STK library
    # holds. The fix for such a library is to keep the model its profile allows
    # and give the drive a vendor id LTFS knows - which is what pairing them
    # here does.
    from ..drives import DriveService

    placement = DriveService(config_directory).placement(library_id)
    if not placement.success:
        report.add('validate', False, placement.message, fatal=True)
        return refuse(placement.message, placement.errors)

    allowed = (placement.data or {}).get('supported') or []
    sibling_model = (placement.data or {}).get('product')
    sibling_vendor = _drive_vendor_of(library_id, conf)

    pairs = _ltfs_pairs(allowed, ltfs_support)
    chosen_vendor = (vendor or '').strip()
    chosen_model = (model or '').strip()

    # A vendor the caller named is never substituted. Quietly choosing a
    # different one would add a drive nobody asked for under a name they did.
    if chosen_vendor and not ltfs_support.usable_drive_models(chosen_vendor):
        report.add('validate', False,
                   f'LTFS does not know the vendor id {chosen_vendor!r}',
                   fatal=True)
        return refuse(
            f'LTFS does not know the vendor id {chosen_vendor!r}',
            [f'it knows: {", ".join(ltfs_support.KNOWN_VENDORS)}',
             'the drive is emulated, so its vendor id is ours to choose',
             'leave vendor out and one LTFS knows is chosen for you'])

    if chosen_vendor and chosen_model:
        pass                                   # both given: checked below
    elif not pairs:
        report.add('validate', False,
                   'no drive this library takes is one LTFS opens', fatal=True)
        return refuse(
            f'Library {library_id} takes no drive LTFS can open',
            [f'it takes: {", ".join(allowed) or "nothing recorded"}',
             'no vendor id LTFS knows lists any of those models',
             'pass vendor= and model= explicitly to override the profile'])
    else:
        # Prefer the model the library's other drives already are - a library of
        # matching drives is worth more than a newer model - and among vendors
        # that can express it, prefer the one the library already uses.
        if chosen_model:
            preferred = [p for p in pairs if p[1] == chosen_model]
        else:
            preferred = [p for p in pairs if p[1] == sibling_model] or pairs
        if chosen_vendor:
            for_vendor = [p for p in preferred if p[0] == chosen_vendor]
            if not for_vendor:
                expressible = sorted(
                    ltfs_support.usable_drive_models(chosen_vendor))
                report.add('validate', False,
                           f'no {chosen_vendor} drive this library takes is one '
                           f'LTFS opens', fatal=True)
                return refuse(
                    f'Library {library_id} takes no {chosen_vendor} drive LTFS '
                    f'can open',
                    [f'it takes: {", ".join(allowed) or "nothing recorded"}',
                     f'LTFS opens these {chosen_vendor} models: '
                     f'{", ".join(expressible)}',
                     'pass model= as well to override the profile'])
            preferred = for_vendor
        if not preferred:
            report.add('validate', False,
                       f'no vendor LTFS knows lists {chosen_model!r}', fatal=True)
            return refuse(
                f'No vendor id LTFS knows lists the model {chosen_model!r}',
                [f'this library takes: {", ".join(allowed)}',
                 'pass vendor= as well to say which id the drive should report'])
        same_vendor = [p for p in preferred if p[0] == sibling_vendor]
        chosen_vendor, chosen_model = (same_vendor or preferred)[0]

    # Validate against the revision the drive will ACTUALLY report, not only one
    # the caller passed. DriveService.add() inherits a sibling's revision, and on
    # library 50 that is `0016` from its STK drives - which fails the IBM LTO-8
    # gate. Without this the drive was written and then the verify step caught
    # it, leaving an added drive LTFS refuses.
    inherited = _inherited_revision(library_id, conf)
    effective = (revision or '').strip() or inherited

    verdict = ltfs_support.supports(chosen_vendor, chosen_model, effective or None)
    if not verdict.supported:
        report.add('validate', False, verdict.reason, fatal=True)
        hints = [verdict.reason]
        if chosen_model not in allowed:
            hints.append(f'this library takes: {", ".join(allowed)}')
        if verdict.firmware_minimum and not revision:
            hints.append(
                f'the drive would inherit revision {inherited!r} from the '
                f"library's other drives; pass a revision of "
                f'{verdict.firmware_minimum} or later')
        return refuse(f'LTFS would not open a {chosen_vendor} {chosen_model}',
                      hints)

    swapped = (sibling_vendor and chosen_vendor != sibling_vendor
               and sibling_vendor not in ltfs_support.KNOWN_VENDORS)

    report.add('validate', True,
               f'LTFS opens {chosen_vendor} {chosen_model}'
               + (f' at revision {effective}' if effective else '')
               + (f' - reported as {chosen_vendor} rather than '
                  f'{sibling_vendor}, which LTFS does not know'
                  if swapped else ''))

    # 2. slots - cartridges need empty ones, and nothing grows a library by
    # itself. Counted before the drive is added so a refusal costs nothing.
    contents = ConfigService(config_directory).library_contents(library_id)
    if contents is None:
        report.add('slots', False, f'library_contents.{library_id} is not readable',
                   fatal=True)
        return refuse(f'Could not read library {library_id}',
                      [f'library_contents.{library_id} is not readable'])

    empty = contents.summary()['empty_slots']
    if tapes > empty:
        if not expand_slots:
            report.add('slots', False,
                       f'{tapes} cartridge(s) wanted, {empty} empty slot(s)',
                       fatal=True)
            return refuse(
                f'Library {library_id} has {empty} empty slot(s), '
                f'{tapes} wanted',
                [f'it needs {tapes - empty} more',
                 'growing a library changes what it advertises to backup '
                 'software, so ask for it explicitly'])
        grown = lifecycle.set_empty_slots(library_id, tapes, config_directory)
        if not grown.success:
            report.add('slots', False, grown.message, fatal=True)
            return refuse(grown.message, grown.errors)
        report.add('slots', True, grown.message)
    else:
        report.add('slots', True,
                   f'{empty} empty slot(s); no change needed'
                   if tapes else f'{empty} empty slot(s)')

    # 3. drive - restart=False, because the slots may have moved too and the
    # library should come back once, not twice.
    added = DriveService(config_directory).add(
        library_id, {'vendor': chosen_vendor, 'product': chosen_model,
                     'revision': revision or ''}, restart=False)
    if not added.success:
        report.add('drive', False, added.message, fatal=True)
        return refuse(added.message, added.errors)
    drive_id = (added.data or {}).get('drive_id')
    report.add('drive', True, added.message)

    # 4. restart - once, now that device.conf and library_contents are both
    # written. The daemons read both at start, and the NEW drive needs its own
    # vtltape@<id> started: restarting the library restarts the robot alone.
    report.add(*_restart_step(library_id, config_directory, service,
                              what='The configuration is',
                              start_drive=drive_id))

    # 5. media - mktape against the media directory; no daemon needed.
    if tapes:
        report.add(*_create_ltfs_media(library_id, tapes, config_directory))

    # 6. record - the same step create_library_workflow ends with, and for the
    # same reason: the web UI reads its drive lists from the database.
    report.add(*_record(library_id, config_directory))

    # 7. verify - ask the running system, rather than reporting success because
    # a file was written.
    report.add(*_verify_ltfs_drive(library_id, drive_id, config_directory))

    return success_result(
        f'Drive {drive_id} added to library {library_id} '
        f'({chosen_vendor} {chosen_model})'
        + (f' with {len(report.warnings)} warning(s)' if report.warnings else ''),
        {**report.to_dict(), 'drive_id': drive_id, 'vendor': chosen_vendor,
         'model': chosen_model, 'revision': revision or '',
         'tapes_requested': tapes}, operation_id)


def _drive_vendor_of(library_id: int, conf) -> str:
    """The vendor id this library's drives already use, or empty."""
    for _, data in sorted(conf.drives_of(library_id).items()):
        vendor = (data.get('vendor') or '').strip()
        if vendor:
            return vendor
    return ''


def _inherited_revision(library_id: int, conf) -> str:
    """The revision an added drive would inherit, by the same rule add() uses.

    The first sibling that reports one, not simply the lowest id - see
    DriveService.add(). Computed here so the firmware gate can be applied before
    anything is written rather than after.
    """
    for _, data in sorted(conf.drives_of(library_id).items()):
        revision = (data.get('revision') or '').strip()
        if revision:
            return revision
    return ''


def _ltfs_pairs(allowed, ltfs_support):
    """(vendor, model) pairs where LTFS knows the vendor and the library takes
    the model.

    Every vendor LTFS knows is considered, not only the library's own. The drive
    is emulated, so its vendor id is ours to choose - and for a StorageTek
    library, choosing one is the entire fix: the model its profile takes is an
    IBM part number that MHVTL emulates, and only the `STK` vendor id is what
    LTFS will not have.
    """
    pairs = []
    for vendor in ltfs_support.KNOWN_VENDORS:
        usable = ltfs_support.usable_drive_models(vendor)
        pairs.extend((vendor, model) for model in allowed if model in usable)
    return pairs


def _create_ltfs_media(library_id: int, count: int, config_directory=None):
    """Cartridges for the drive just added, reported as one step."""
    from ..tapes import TapeService

    try:
        result = TapeService(config_directory).create_bulk(library_id, count)
    except Exception as exc:                           # noqa: BLE001 - reported
        logger.exception('creating %s cartridge(s) in library %s', count, library_id)
        return ('media', False, f'The drive was added, but its cartridges were '
                                f'not created: {exc}')
    return ('media', result.success,
            result.message if result.success
            else f'The drive was added, but its cartridges were not: '
                 f'{result.message}')


def _verify_ltfs_drive(library_id: int, drive_id, config_directory=None):
    """Would LTFS open the drive that now exists?

    Reads it back out of device.conf rather than trusting what was asked for,
    because the point of the step is to catch the case where the file says
    something other than the request - a truncated write, a profile cascade that
    substituted a model, a revision that did not survive.
    """
    from ..config.service import ConfigService
    from ..profiles import ltfs_support

    conf = ConfigService(config_directory).device_conf()
    written = (conf.drives_of(library_id).get(drive_id) or {}) if conf else {}
    if not written:
        return ('verify', False,
                f'Drive {drive_id} is not in device.conf after being added')

    verdict = ltfs_support.supports(written.get('vendor', ''),
                                    written.get('product', ''),
                                    written.get('revision') or None)
    if not verdict.supported:
        return ('verify', False,
                f'Drive {drive_id} was added but LTFS will not open it: '
                f'{verdict.reason}')
    return ('verify', True,
            f'LTFS can open drive {drive_id}: {written.get("vendor", "")} '
            f'{written.get("product", "")}'
            + (f' rev {written["revision"]}' if written.get('revision') else ''))

# -- cartridges an LTFS drive can format ------------------------------------
#
# The drive comes first, always. A library with no LTFS-capable drive is refused
# here rather than warned at, because creating LTO-8 cartridges for a library
# that cannot mount them leaves an operator believing they have finished when
# they have not.
#
# This creates BLANK media of a density LTFS can work with. It does not format
# anything: a cartridge becomes an LTFS volume through mkltfs, which erases it
# and is gated twice over on purpose.

def add_ltfs_media_workflow(library_id: int, count: int, *,
                            density: str = None, expand_slots: bool = False,
                            config_directory=None) -> ServiceResult:
    """Create cartridges an LTFS-capable drive in this library can format.

    Usage::

        result = add_ltfs_media_workflow(60, 2)
        for step in result.data['steps']:
            print(step['step'], step['message'])

    density defaults to the newest one the library's LTFS drive can write - see
    profiles/ltfs_support.ltfs_media_for(), which is where "can this cartridge
    hold an LTFS volume in this drive" is decided. Passing a density it cannot
    format is refused with the reason.

    expand_slots must be asked for. A library's slot count is part of what it
    advertises to backup software, and growing it because somebody wanted two
    cartridges is not ours to decide.

    Composes TapeService.create_bulk() and lifecycle.set_empty_slots(); no
    barcode logic and no slot logic of its own.
    """
    from ..config.service import ConfigService
    from ..profiles import ltfs_support
    from ..tapes import TapeService
    from . import lifecycle

    operation_id = str(uuid.uuid4())[:8]
    library_id = int(library_id)
    report = WorkflowReport()
    report.library_id = library_id

    def refuse(message, errors):
        result = failure_result(message, errors, operation_id)
        result.data = report.to_dict()
        return result

    if int(count) < 1:
        report.add('validate', False, 'No cartridges asked for', fatal=True)
        return refuse('Ask for at least one cartridge', ['nothing to do'])
    count = int(count)

    # 1. validate - the drive comes first, and the density has to be one that
    # drive can actually write.
    config = ConfigService(config_directory)
    conf = config.device_conf()
    if conf is None or library_id not in conf.libraries:
        report.add('validate', False,
                   f'Library {library_id} is not in device.conf', fatal=True)
        return refuse(f'Library {library_id} is not in device.conf',
                      ['cartridges can only be added to a library that exists'])

    drive = _ltfs_drive_of(library_id, conf, ltfs_support)
    if drive is None:
        report.add('validate', False,
                   'no drive in this library can open LTFS', fatal=True)
        return refuse(
            f'Library {library_id} has no drive LTFS can open',
            ['add one first: mhvtl ltfs add-drive '
             f'{library_id}, or the LTFS page',
             'cartridges LTFS cannot mount are not worth creating'])
    drive_id, drive_product = drive

    offered = ltfs_support.ltfs_media_for(drive_product)
    usable = [m for m in offered if m.usable]
    if not usable:
        report.add('validate', False,
                   f'{drive_product} can format no density this profile offers',
                   fatal=True)
        return refuse(
            f'{drive_product} can format none of its own media',
            [f'{m.density}: {m.reason}' for m in offered] or
            ['MHVTL gives this drive model no media list'])

    if density:
        wanted = density.strip().upper()
        match = next((m for m in offered if m.density == wanted), None)
        if match is None:
            report.add('validate', False,
                       f'{drive_product} does not handle {wanted}', fatal=True)
            return refuse(
                f'Drive {drive_id} ({drive_product}) does not handle {wanted}',
                [f'it can format: {", ".join(m.density for m in usable)}'])
        if not match.usable:
            report.add('validate', False, match.reason, fatal=True)
            return refuse(
                f'{wanted} cannot hold an LTFS volume in drive {drive_id}',
                [match.reason,
                 f'it can format: {", ".join(m.density for m in usable)}'])
        chosen = wanted
    else:
        chosen = usable[0].density

    report.add('validate', True,
               f'drive {drive_id} ({drive_product}) can format {chosen}'
               + ('' if density else ' - the newest density it writes'))

    # 2. slots - counted before anything is created, so a refusal costs nothing.
    contents = config.library_contents(library_id)
    if contents is None:
        report.add('slots', False,
                   f'library_contents.{library_id} is not readable', fatal=True)
        return refuse(f'Could not read library {library_id}',
                      [f'library_contents.{library_id} is not readable'])

    empty = contents.summary()['empty_slots']
    if count > empty:
        if not expand_slots:
            report.add('slots', False,
                       f'{count} cartridge(s) wanted, {empty} empty slot(s)',
                       fatal=True)
            return refuse(
                f'Library {library_id} has {empty} empty slot(s), {count} wanted',
                [f'it needs {count - empty} more',
                 'growing a library changes what it advertises to backup '
                 'software, so ask for it explicitly'])
        grown = lifecycle.set_empty_slots(library_id, count, config_directory)
        if not grown.success:
            report.add('slots', False, grown.message, fatal=True)
            return refuse(grown.message, grown.errors)
        report.add('slots', True, grown.message)
    else:
        report.add('slots', True,
                   f'{empty} empty slot(s); no change needed')

    # 3. media - create_bulk owns the barcodes, the density suffix and the
    # media files. Nothing about any of that is repeated here.
    created = TapeService(config_directory).create_bulk(
        library_id, count, density=chosen)
    if not created.success:
        report.add('media', False, created.message, fatal=True)
        return refuse(created.message, created.errors)
    report.add('media', True, created.message)

    # 4. restart - library_contents has changed, and vtllibrary reads it once at
    # start, so the robot does not report the new cartridges until it restarts.
    # Once, after every write, as in the drive workflow.
    report.add(*_restart_step(library_id, config_directory,
                              LibraryService(config_directory),
                              what='The cartridges are'))

    # 5. record - the database caches the slot and media counts from
    # library_contents, and both have just moved.
    report.add(*_record(library_id, config_directory))

    # 6. verify - read the file back rather than trusting what was asked for,
    # by NAME rather than by count, and say plainly what these are not yet.
    # create_bulk reports them under 'created', each a create() payload. The
    # first version of this read 'tapes', got an empty list, and the verify step
    # below passed on nothing - a check that cannot fail is worse than none.
    barcodes = [row.get('barcode') for row in (created.data or {}).get('created', [])
                if row.get('barcode')]
    report.add(*_verify_ltfs_media(library_id, chosen, barcodes,
                                   config_directory))
    return success_result(
        f'{count} {chosen} cartridge(s) created in library {library_id}'
        + (f' with {len(report.warnings)} warning(s)' if report.warnings else ''),
        {**report.to_dict(), 'density': chosen, 'count': count,
         'barcodes': barcodes, 'drive_id': drive_id,
         'drive_product': drive_product}, operation_id)


def _ltfs_drive_of(library_id: int, conf, ltfs_support):
    """(drive_id, product) of the first drive in this library LTFS would open.

    Lowest drive id first, so the answer is stable. The revision is passed, so a
    drive that is the right model with firmware too old is not counted - which
    is the whole point of having read it out of device.conf.
    """
    for drive_id, data in sorted(conf.drives_of(library_id).items()):
        verdict = ltfs_support.supports(data.get('vendor', ''),
                                        data.get('product', ''),
                                        data.get('revision') or None)
        if verdict.supported:
            return drive_id, data.get('product', '')
    return None


def _verify_ltfs_media(library_id: int, density: str, barcodes: List[str],
                       config_directory=None):
    """Are the cartridges just created actually in the file, and what are they not?

    Checks the BARCODES create_bulk reported, not a count of matching suffixes.
    Counting was the first version and it could not fail: library 60 already
    held five L7 cartridges, so "at least two exist" passed while nothing had
    been written. A named barcode either is in the file or is not.

    And it says plainly what these are: blank media of a density LTFS can work
    with. They are not LTFS volumes and will not mount until mkltfs has
    formatted one, which erases it. That sentence is the difference between an
    operator who knows what they have and one who thinks the library is ready.
    """
    from ..config.service import ConfigService

    contents = ConfigService(config_directory).library_contents(library_id)
    if contents is None:
        return ('verify', False,
                f'library_contents.{library_id} could not be read back')

    if not barcodes:
        # Nothing to look for means nothing was reported created, and a check
        # with no subject must not report success.
        return ('verify', False,
                'the creation reported no barcodes, so nothing could be verified')

    listed = {b.strip().upper() for b in contents.barcodes}
    missing = [b for b in barcodes if b.strip().upper() not in listed]
    if missing:
        return ('verify', False,
                f'{len(missing)} of {len(barcodes)} cartridge(s) are not in '
                f'library_contents.{library_id}: {", ".join(missing[:5])}')
    return ('verify', True,
            f'{len(barcodes)} {density} cartridge(s) in slots - blank media, '
            f'not LTFS volumes: mkltfs formats one, and erases it')

# -- what a library needs before LTFS can be used on it ---------------------
#
# One call the page and the CLI both make, composed from what already answers:
# DriveService.placement() for the drive side, ConfigService for the slots,
# profiles/ltfs_support for what LTFS accepts, and tapes/ltfs_state for which
# cartridges are already LTFS volumes. It adds NO new source of truth - if two
# numbers here ever disagree with a page elsewhere, this is the wrong place to
# fix it.
#
# It lives in libraries/ rather than ltfs/ on purpose. It needs
# DriveService.placement(), and ltfs/ -> drives/ would be a new edge in the
# dependency graph; libraries/ already imports drives/. ltfs/ stays about
# mounting, configuring a library stays here, and the page calls both - see
# rule 7 in services/__init__.py.

def ltfs_provisioning(library_id: int, config_directory=None) -> ServiceResult:
    """What this library has, what it is missing, and what it could be given.

    Reading only; writes nothing and starts nothing::

        result.data = {
            'library': {'vendor', 'product'},
            'drives': [{drive_id, vendor, product, revision, ltfs_capable,
                        ltfs_reason}],
            'ltfs_drives': n,
            'needs': ['drive'] | ['media'] | ['drive', 'media'] | [],
            'can_add_drive': bool, 'cannot_add_because': str,
            'drive_candidates': [{vendor, model, firmware_minimum}],
            'drive_slots': {'used', 'max'},
            'storage_slots': {'empty', 'full', 'total'},
            'media_candidates': [{density, suffix, usable, reason}],
            'ltfs_media': n, 'ltfs_media_barcodes': [...],
        }

    `needs` is the order of the flow: a drive before cartridges, always. An
    empty list means the library is provisioned and the page shows nothing.
    """
    from ..config.service import ConfigService
    from ..drives import DriveService
    from ..profiles import ltfs_support
    from ..tapes import ltfs_state, media as tape_media

    operation_id = str(uuid.uuid4())[:8]
    library_id = int(library_id)
    config = ConfigService(config_directory)

    conf = config.device_conf()
    declared = conf.libraries.get(library_id) if conf else None
    if declared is None:
        return failure_result(f'Library {library_id} is not in device.conf',
                              ['nothing can be added to a library that does '
                               'not exist'], operation_id)

    # -- the drives it has, and whether LTFS would open each ----------------
    drives = []
    for drive_id, data in sorted(conf.drives_of(library_id).items()):
        verdict = ltfs_support.supports(data.get('vendor', ''),
                                        data.get('product', ''),
                                        data.get('revision') or None)
        drives.append({
            'drive_id': drive_id,
            'vendor': data.get('vendor', ''),
            'product': data.get('product', ''),
            'revision': data.get('revision') or '',
            'ltfs_capable': verdict.supported,
            'ltfs_reason': verdict.reason,
        })
    capable = [d for d in drives if d['ltfs_capable']]

    # -- what could be added, and whether there is room ---------------------
    placement = DriveService(config_directory).placement(library_id)
    plan = (placement.data or {}) if placement.success else {}
    allowed = plan.get('supported') or []
    candidates = []
    for vendor, model in _ltfs_pairs(allowed, ltfs_support):
        family = ltfs_support.usable_drive_models(vendor).get(model)
        candidates.append({
            'vendor': vendor, 'model': model,
            'firmware_minimum': ltfs_support.firmware_minimum_for(family, vendor)
                                or '',
        })

    can_add, because = True, ''
    if not placement.success:
        can_add, because = False, placement.message
    elif plan.get('full'):
        can_add, because = False, (
            f'the library is full: {plan.get("layout", "this model")} holds at '
            f'most {plan.get("max_drives")} drives')
    elif plan.get('drive_id') is None:
        can_add, because = False, 'no drive id is free'
    elif plan.get('target') is None:
        can_add, because = False, 'no SCSI target is free in device.conf'
    elif not candidates:
        can_add, because = False, (
            'no drive this library takes is one LTFS can open'
            + (f' - it takes: {", ".join(allowed)}' if allowed else ''))

    # -- the cartridges it has, and which densities it could be given -------
    contents = config.library_contents(library_id)
    summary = contents.summary() if contents else {}
    barcodes = list(contents.barcodes) if contents else []

    # Which cartridges are LTFS volumes costs a read of every cartridge's own
    # memory, so it is asked ONLY when the answer can be used: a library with
    # no LTFS-capable drive needs a drive first, whatever its cartridges say.
    # The same gate as TapeService.list(), for the same reason - library 30 has
    # 40 cartridges and no capable drive, and reading all 40 to print a number
    # nobody can act on is what gate 1 exists to prevent.
    #
    # None, not 0: nothing was read, so nothing is claimed. See the NOT_ASKED
    # state in tapes/ltfs_state.py - "we did not look" is not "there are none".
    ltfs_media, ltfs_barcodes = None, []
    if capable and barcodes:
        usage = tape_media.usage_for_all(barcodes)
        states = ltfs_state.state_for_all(
            barcodes, {b: u.partitions for b, u in usage.items()})
        ltfs_barcodes = sorted(b for b, s in states.items()
                               if s.state == ltfs_state.LTFS)
        ltfs_media = len(ltfs_barcodes)
    elif capable:
        ltfs_media = 0

    # The media a cartridge could be created as is decided by the LTFS-capable
    # drive the library HAS - the drive is always added first, so there is no
    # case where this has to be derived from a drive that does not exist yet.
    media_candidates = []
    if capable:
        media_candidates = [m.to_dict() for m in
                            ltfs_support.ltfs_media_for(capable[0]['product'])]

    # The order of the flow. 'media' is only ever asked for once a drive exists,
    # because until then the cartridges have not been read and there is nothing
    # to base it on.
    needs = []
    if not capable:
        needs.append('drive')
    elif not ltfs_media:
        needs.append('media')

    # Whether cartridges could be added at all - the drive and a density it can
    # write. Whether there are enough empty slots depends on how many the
    # operator asks for, so that arithmetic stays in add_ltfs_media_workflow(),
    # which knows the count. The empty count is reported so a form can warn.
    usable_density = [m for m in media_candidates if m['usable']]
    if not capable:
        can_add_media, media_because = False, (
            'add a drive LTFS can open first; the drive decides which '
            'cartridges can be formatted')
    elif not usable_density:
        can_add_media, media_because = False, (
            f'{capable[0]["product"]} can format none of the media this '
            f'library offers')
    else:
        can_add_media, media_because = True, ''

    return success_result(
        _provisioning_message(library_id, capable, ltfs_media, needs),
        {'library_id': library_id,
         'library': {'vendor': declared.get('vendor', ''),
                     'product': declared.get('product', '')},
         'drives': drives,
         'ltfs_drives': len(capable),
         'needs': needs,
         'can_add_drive': can_add,
         'cannot_add_because': because,
         'drive_candidates': candidates,
         'drive_slots': {'used': plan.get('drives_now', len(drives)),
                         'max': plan.get('max_drives')},
         'storage_slots': {'empty': summary.get('empty_slots', 0),
                           'full': summary.get('full_slots', 0),
                           'total': summary.get('total_slots', 0)},
         'media_candidates': media_candidates,
         'can_add_media': can_add_media,
         'cannot_add_media_because': media_because,
         'ltfs_media': ltfs_media,
         'ltfs_media_barcodes': ltfs_barcodes},
        operation_id)


def _provisioning_message(library_id, capable, ltfs_media, needs) -> str:
    """One sentence, so the page, the CLI and a log all say the same thing."""
    if not needs:
        return (f'Library {library_id} is ready for LTFS: '
                f'{len(capable)} drive(s) LTFS can open and '
                f'{ltfs_media} LTFS volume(s)')
    if needs == ['drive']:
        return (f'Library {library_id} has no drive LTFS can open, so its '
                f'cartridges were not read')
    if needs == ['media']:
        return (f'Library {library_id} has {len(capable)} drive(s) LTFS can '
                f'open and no LTFS volumes')
    return f'Library {library_id} has no drive LTFS can open'
