"""Library listing and lifecycle.

Reading is implemented here, on services/config. Creating, updating and
deleting still delegate to MHVTLLibraryService, deliberately:

    create_library there is around 250 lines of vendor-profile validation -
    which media a model supports, which drive a library may carry, how a serial
    is truncated - and it is the most complete version in the tree. Moving it
    wholesale into a new module would mean re-deriving those rules from a table
    of vendor quirks, which is how the two earlier forks of this file started.
    It is absorbed in a later step, with the profile rules moved to
    services/profiles first.

What this module does own is the shape of the answer: everything returns a
ServiceResult, so a caller cannot tell from the result which implementation
happened to run.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..config import device_conf as device_conf_format
from ..config import ids
from ..config import library_contents as contents_format
from ..core import (ServiceResult, SLOW, config_dir, device_conf_path,
                    failure_result, library_contents_path, shell, success_result)
from ..console import units
from ..iscsi import bindings as iscsi_bindings
from . import lifecycle
from . import validation as validation_rules
from .models import LibraryInfo

logger = logging.getLogger(__name__)


class LibraryService:
    """Libraries, as device.conf and library_contents describe them.

        LibraryService().list()
        LibraryService().get(10)
    """

    def __init__(self, config_directory=None):
        self.config_dir = Path(config_directory) if config_directory else config_dir()

    # -- reading ----------------------------------------------------------

    def _conf(self) -> Optional[device_conf_format.DeviceConf]:
        result = shell.sudo_cat(device_conf_path(self.config_dir))
        if not result.ok:
            logger.warning('cannot read device.conf: %s', result.stderr.strip()[:200])
            return None
        return device_conf_format.parse(result.stdout)

    def _contents(self, library_id: int):
        result = shell.sudo_cat(library_contents_path(library_id, self.config_dir))
        return contents_format.parse(result.stdout) if result.ok else None

    def list(self, *, with_contents: bool = True) -> ServiceResult:
        """Every configured library.

        with_contents=False skips reading each library_contents file, for callers
        that only want the models and drive counts.
        """
        operation_id = str(uuid.uuid4())[:8]
        conf = self._conf()
        if conf is None:
            return failure_result(
                'Could not read the library configuration',
                [f'{device_conf_path(self.config_dir)} is not readable'], operation_id)

        libraries = []
        for library_id, data in conf.libraries.items():
            info = LibraryInfo.from_config(
                library_id, data, list(conf.drives_of(library_id)))
            if with_contents:
                contents = self._contents(library_id)
                if contents is not None:
                    info.slot_count = len(contents.slots)
                    info.tape_count = len(contents.occupied)
            libraries.append(info)

        libraries.sort(key=lambda item: item.library_id)
        return success_result(
            f'{len(libraries)} librar{"ies" if len(libraries) != 1 else "y"} configured',
            {'libraries': [item.to_dict() for item in libraries],
             'count': len(libraries)}, operation_id)

    def summary(self) -> ServiceResult:
        """What this host holds: libraries, drives, cartridges, slots.

        One answer, from ``list()``, which reads ``device.conf`` and the
        contents files - so these are the numbers the host has rather than
        the numbers the database remembers. That distinction is the reason
        this exists. The web console's panel counted rows in the database
        instead, through ``discovery_status`` buckets that nothing sets and a
        ``media_found`` that was the literal 0, and answered 7 libraries, 21
        drives and 0 cartridges for a host with 156 of them.

        ``says`` is the sentence, composed here because both front ends print
        it: ``mhvtl library list`` ends with it and the console shows it above
        its libraries.
        """
        operation_id = str(uuid.uuid4())[:8]
        listed = self.list()
        if not listed.success:
            return listed

        rows = listed.data['libraries']

        def total(key):
            """The ones that are known. A library whose contents file cannot
            be read has None here, not 0 - and the two are different
            answers."""
            return sum(int(row[key]) for row in rows if row[key] is not None)

        counts = {
            'libraries': len(rows),
            'drives': total('drives'),
            'cartridges': total('tape_count'),
            'slots': total('slot_count'),
            #: Libraries whose contents could not be read. Their cartridges
            #: are not counted above, so the sentence says so rather than
            #: letting a missing file look like an empty library.
            'unreadable': sum(1 for row in rows if row['tape_count'] is None),
        }
        counts['free_slots'] = max(counts['slots'] - counts['cartridges'], 0)

        if not rows:
            says = 'No libraries on this host yet.'
        else:
            says = (f"{counts['libraries']} librar"
                    f"{'ies' if counts['libraries'] != 1 else 'y'}, "
                    f"{counts['drives']} drive(s), "
                    f"{counts['cartridges']} cartridge(s) "
                    f"in {counts['slots']} slot(s) - "
                    f"{counts['free_slots']} free")
            if counts['unreadable']:
                says += (f" ({counts['unreadable']} librar"
                         f"{'ies' if counts['unreadable'] != 1 else 'y'} "
                         f"could not be read)")
        return success_result(says, {**counts, 'says': says}, operation_id)

    def get(self, library_id: int) -> ServiceResult:
        """One library, with its slot and tape counts."""
        operation_id = str(uuid.uuid4())[:8]
        conf = self._conf()
        if conf is None:
            return failure_result('Could not read the library configuration',
                                  ['device.conf is not readable'], operation_id)

        data = conf.libraries.get(library_id)
        if data is None:
            return failure_result(f'Library {library_id} not found',
                                  [f'no Library: {library_id} entry in device.conf'],
                                  operation_id)

        info = LibraryInfo.from_config(library_id, data, list(conf.drives_of(library_id)))
        contents = self._contents(library_id)
        if contents is not None:
            info.slot_count = len(contents.slots)
            info.tape_count = len(contents.occupied)

        return success_result(f'Library {library_id}: {info.model}',
                              {'library': info.to_dict()}, operation_id)

    def config_text(self, library_id: int) -> ServiceResult:
        """A library's device.conf record and its drives' records, verbatim."""
        operation_id = str(uuid.uuid4())[:8]
        read = shell.sudo_cat(device_conf_path(self.config_dir))
        if not read.ok:
            return failure_result('Could not read the library configuration',
                                  ['device.conf is not readable'], operation_id)
        text = read.stdout
        conf = device_conf_format.parse(text)
        library = device_conf_format.record_text(text, 'Library', library_id)
        if library is None:
            return failure_result(f'Library {library_id} not found',
                                  [f'no Library: {library_id} entry in device.conf'],
                                  operation_id)
        drives = [device_conf_format.record_text(text, 'Drive', drive_id) or ''
                  for drive_id in sorted(conf.drives_of(library_id))]
        return success_result(f'Library {library_id} configuration',
                              {'text': library + ''.join(drives)}, operation_id)

    def next_id(self, *, step: int = 10) -> ServiceResult:
        """The next free library id: the lowest free multiple of ten.

        Free across libraries *and* drives - they share MHVTL's id namespace -
        and below 1024, which both daemons refuse. config/ids has the rules;
        this used to count upward with no limit and check libraries only.
        """
        operation_id = str(uuid.uuid4())[:8]
        conf = self._conf() or ids.empty_conf()
        candidate = ids.next_library_id(conf, step=step)
        if candidate is None:
            return failure_result(
                'No library id is free',
                [f'every multiple of {step} up to '
                 f'{ids.MAX_DEVICE_ID} is in use'], operation_id)
        return success_result(f'Next free library id is {candidate}',
                              {'library_id': candidate,
                               'used': sorted(conf.libraries)}, operation_id)

    # -- lifecycle ---------------------------------------------------------

    def create(self, spec: Dict, *, start_services: bool = True) -> ServiceResult:
        """Create a library, its drives and its media slots.

        The specification needs only a vendor profile and, unless one is to be
        allocated, a library id; lifecycle.create fills in the rest from the
        profile and validates the result before writing anything.
        """
        return lifecycle.create(spec, self.config_dir, next_id=self._allocate_id,
                                start_services=start_services)

    def _allocate_id(self) -> int:
        """next_id() for spec.apply_defaults, which wants a number or a refusal."""
        from . import spec as spec_rules

        result = self.next_id()
        if not result.success:
            raise spec_rules.UnknownProfile('; '.join(result.errors) or result.message)
        return result.data['library_id']

    def update(self, library_id: int, spec: Dict) -> ServiceResult:
        """Change a library by deleting and recreating it.

        MHVTL reads a library's vendor, product and drive count once at daemon
        start, so there is no in-place change; lifecycle.update says what that
        costs, and library_contents is regenerated.
        """
        return lifecycle.update(library_id, spec, self.config_dir)

    def delete(self, library_id: int, *, force: bool = False,
               remove_media: bool = False) -> ServiceResult:
        """Remove a library and its drives.

        Refuses while a drive still has a tape loaded unless force is set; keeps
        the media unless remove_media is set.
        """
        # Before the daemons stop: a device removed while an iSCSI backstore
        # holds it is never freed, and poisons its address until a reboot.
        exports = iscsi_bindings.release_library(library_id,
                                                 config_directory=self.config_dir)
        if not exports.success:
            return failure_result(
                f'Library {library_id} was not removed: {exports.message}',
                exports.errors + ['delete its backstores under iSCSI > Backstores, '
                                  'then try again'], exports.operation_id)
        result = lifecycle.delete(library_id, force=force, remove_media=remove_media,
                                  config_directory=self.config_dir)
        if result.success and exports.data.get('released'):
            result.message += f'; {exports.message}'
        return result

    def validate(self, spec: Dict) -> ServiceResult:
        """Check a library specification without creating anything.

        Implemented here now: validation.validate applies the vendor-profile
        rules from services/profiles and no longer goes through the delegated
        service.
        """
        operation_id = str(uuid.uuid4())[:8]
        validation = validation_rules.validate(spec, self.config_dir)
        if getattr(validation, 'is_valid', False):
            return success_result('Specification is valid',
                                  {'warnings': list(getattr(validation, 'warnings', []))},
                                  operation_id)

        # The suggested fixes travel with the errors. validation.validate works
        # out the valid values whenever it rejects one - "Valid drive models for
        # IBM: ..." beside "Drive model 'T10000C' is not valid for IBM" - and
        # this method used to take the errors and leave the fixes behind, so an
        # operator was shown the problem and denied the answer the service had
        # already computed. lifecycle.create next door has always sent both.
        #
        # Appended rather than interleaved: errors say what is wrong, fixes say
        # what would be right, and a caller printing them in order reads
        # correctly either way.
        return failure_result(
            'Specification is not valid',
            list(getattr(validation, 'errors', []))
            + list(getattr(validation, 'suggested_fixes', [])),
            operation_id)

    def restart_services(self, library_id: int = None) -> ServiceResult:
        """Restart the daemons, so a configuration change takes effect."""
        operation_id = str(uuid.uuid4())[:8]
        try:
            outcome = units.restart_library(library_id)
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('restarting mhvtl services')
            return failure_result(f'Could not restart the MHVTL services: {exc}',
                                  [str(exc)], operation_id)

        if not outcome['ok']:
            return failure_result(f'Could not restart {outcome["restarted"]}',
                                  [outcome['error'] or 'systemctl reported a '
                                   'failure'], operation_id)
        message = f'Restarted {outcome["restarted"]}'
        # A restart re-creates the devices, and an iSCSI export keeps the old
        # ones until it is rebound.
        exports = iscsi_bindings.after_restart(library_id, config_directory=self.config_dir)
        if exports:
            outcome['iscsi'] = exports
            message += f'; {exports}'
        return success_result(message, outcome, operation_id)

    def recognised_by_mhvtl(self, library_id: int) -> bool:
        """Has the running system actually picked the library up?"""
        try:
            return lifecycle.recognised(library_id, self.config_dir)
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.warning('checking whether MHVTL sees library %s: %s',
                           library_id, exc)
            return False

