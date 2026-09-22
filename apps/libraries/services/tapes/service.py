"""Tape create, bulk create, delete and list.

Moved from tape_operations_service.py:1934 (create_tape), :2086 (delete_tape),
:2179 (list_tapes) and :2229 (create_tapes_bulk), with the library_contents
editing that went with them.

A tape is only created in a density one of the library's drives loads, and
with a barcode whose suffix names that density: MHVTL's drives refuse other
media on load, and its tools read the density back from the suffix.

The order of operations on create is deliberate: validate, check the slot is
free and the barcode unused, make the media, then record it in
library_contents. A failure after the media is made leaves a file with no slot,
which an operator can see and clean up; a failure the other way round leaves a
slot pointing at a tape that does not exist, which the library reports as a read
error at the worst moment.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..config import library_contents as contents_format
from ..config.service import ConfigService
from ..profiles import data as profiles_data
from ..profiles import personalities
from ..core import (FileLock, ServiceResult, config_dir, failure_result,
                    library_contents_path, lock_path, shell, success_result)
from . import barcodes, media
from .models import TapeInfo

#: library_contents.10 -> 10, as in libraries/orphans.
CONTENTS_RE = re.compile(r'^library_contents\.(\d+)$')

logger = logging.getLogger(__name__)

DEFAULT_SIZE_MB = 500
DEFAULT_DENSITY = 'LTO8'


class TapeService:
    """Tapes, as library_contents records them and the media directory holds them.

        TapeService().list(10)
        TapeService().create(10, 'E01033L8', slot=33)
    """

    def __init__(self, config_directory=None, media_directory=None):
        self.config_dir = Path(config_directory) if config_directory else config_dir()
        self.media_dir = media_directory

    # -- reading ----------------------------------------------------------

    def _contents(self, library_id: int):
        result = shell.sudo_cat(library_contents_path(library_id, self.config_dir))
        if not result.ok:
            return None
        return contents_format.parse(result.stdout)

    def list(self, library_id: int, *, with_usage: bool = True) -> ServiceResult:
        """Every tape in a library.

        with_usage=False skips the disk measurement entirely, for callers that
        only need to know what is in which slot.
        """
        operation_id = str(uuid.uuid4())[:8]
        contents = self._contents(library_id)
        if contents is None:
            return failure_result(
                f'Could not read library {library_id}',
                [f'{library_contents_path(library_id, self.config_dir)} is not '
                 f'readable'], operation_id)

        tapes = [TapeInfo(barcode=slot.barcode, library_id=library_id,
                          slot=slot.number)
                 for slot in contents.occupied]

        if with_usage and tapes:
            # One pass over the media directory, not two sudo calls per tape.
            usage = media.usage_for_all([t.barcode for t in tapes], self.media_dir)
            for tape in tapes:
                found = usage.get(tape.barcode)
                if found:
                    tape.used_mb = found.used_mb
                    tape.capacity_mb = found.capacity_mb
                    tape.media_exists = found.exists
                    tape.layout = found.layout

        return success_result(
            f'{len(tapes)} tape{"s" if len(tapes) != 1 else ""} in library {library_id}',
            {'library_id': library_id, 'count': len(tapes),
             'tapes': [tape.to_dict() for tape in tapes],
             'summary': contents.summary()}, operation_id)

    def barcodes_in(self, library_id: int) -> List[str]:
        contents = self._contents(library_id)
        return contents.barcodes if contents else []

    def next_barcode(self, library_id: int, *, prefix: str = None,
                     suffix: str = None) -> ServiceResult:
        """The next free barcode in the series this library already uses."""
        operation_id = str(uuid.uuid4())[:8]
        existing = self.barcodes_in(library_id)

        prefix = prefix or barcodes.detect_prefix(existing)
        suffix = suffix or barcodes.detect_suffix(existing)
        if not prefix:
            return failure_result(
                f'Cannot tell what barcode series library {library_id} uses',
                ['the library has no data tapes yet; pass a prefix'], operation_id)

        number = barcodes.next_number(existing, prefix, suffix)
        return success_result(
            f'Next free barcode is {barcodes.build(prefix, number, suffix)}',
            {'barcode': barcodes.build(prefix, number, suffix), 'prefix': prefix,
             'suffix': suffix, 'number': number}, operation_id)

    def barcode_prefix(self, library_id: int, vendor: str = None) -> str:
        """The three-character prefix this library's tapes use.

        What the tapes already in it use, falling back to the vendor profile's
        leading letter and the library id - the same rule the create form
        applies. It lived in two views, each with its own copy of the profile
        lookup; a library's barcode series is a fact about the library, so it
        belongs here.
        """
        computed = f'L{int(library_id):02d}'[:3]
        vendor = vendor or self._vendor_of(library_id)
        if vendor:
            try:
                profile = profiles_data.get_profile(vendor.upper())
                computed = (profile.barcode_leading + f'{int(library_id):02d}')[:3]
            except (KeyError, ValueError):
                pass
        return barcodes.detect_prefix(self.barcodes_in(library_id), computed)

    def _vendor_of(self, library_id: int) -> str:
        """The library's vendor, as device.conf declares it."""
        conf = ConfigService(self.config_dir).device_conf()
        library = conf.libraries.get(int(library_id)) if conf else None
        return (library or {}).get('vendor', '')

    def media_for_library(self, library_id: int) -> Dict:
        """Which tapes this library's drives can use.

        {'drives': [product, ...], 'known': bool, 'default': density or None,
         'media': [{'density', 'suffix', 'writable', 'writable_in',
                    'read_only_in'}, ...]}

        Media are in the order the drives list them, native first, so the
        first is the natural default. known is False when device.conf cannot
        be read or the library has no drive MHVTL gives a media list; callers
        then offer everything and let MHVTL decide.
        """
        conf = ConfigService(self.config_dir).device_conf()
        drives = ([data.get('product', '') for _, data in
                   sorted(conf.drives_of(int(library_id)).items())]
                  if conf else [])
        media_list, seen = [], {}
        for product in drives:
            support = personalities.drive_media(product)
            if support is None:
                continue
            for density in personalities.creatable_media(product):
                entry = seen.get(density)
                if entry is None:
                    entry = seen[density] = {
                        'density': density,
                        'suffix': personalities.SUFFIX_BY_DENSITY[density],
                        'writable_in': [], 'read_only_in': []}
                    media_list.append(entry)
                bucket = ('writable_in' if density in support.read_write
                          else 'read_only_in')
                if product not in entry[bucket]:
                    entry[bucket].append(product)
        for entry in media_list:
            entry['writable'] = bool(entry['writable_in'])
        writable = [m['density'] for m in media_list if m['writable']]
        return {'library_id': library_id, 'drives': drives,
                'known': bool(media_list), 'media': media_list,
                'default': writable[0] if writable else None}

    def check_media(self, library_id: int, density: str,
                    barcode: str = None) -> Optional[str]:
        """Why a tape of this density cannot be made here, or None if it can.

        Refuses a density mktape does not accept, a barcode whose suffix names
        a different density, and a density none of the library's drives load.
        A library whose drives are unknown is not refused.
        """
        if density not in personalities.SUFFIX_BY_DENSITY:
            return (f'{density} is not a density this application can create; '
                    f'use one of {", ".join(personalities.SUFFIX_BY_DENSITY)}')
        declared = personalities.density_for_barcode(barcode) if barcode else None
        if barcode and not barcode.startswith(barcodes.CLEANING_PREFIX) \
                and declared and declared != density:
            return (f'{barcode} ends in {barcode[-2:]}, which is {declared}, not '
                    f'{density}; use suffix '
                    f'{personalities.SUFFIX_BY_DENSITY[density]}')
        library = self.media_for_library(library_id)
        if library['known'] and density not in {m['density'] for m in library['media']}:
            offered = ', '.join(m['density'] for m in library['media'])
            return (f'No drive in library {library_id} loads {density} tapes; '
                    f'its drives take {offered}')
        return None

    # -- writing ----------------------------------------------------------

    def create(self, library_id: int, barcode: str, *, slot: int = None,
               size_mb: int = DEFAULT_SIZE_MB, density: str = None,
               kind: str = None, check_media: bool = True) -> ServiceResult:
        """Create one tape and put it in a slot.

        The density defaults to what the barcode's suffix says, then to the
        library's first writable medium. check_media=False skips the drive
        check, for create_bulk, which has already made it once for the run.
        """
        operation_id = str(uuid.uuid4())[:8]

        try:
            barcode = barcodes.validate((barcode or '').upper())
        except barcodes.InvalidBarcode as exc:
            return failure_result(f'Invalid barcode: {exc}', [str(exc)], operation_id)

        density = (density or '').upper() or barcodes.density_for(barcode) \
            or self.media_for_library(library_id)['default'] or DEFAULT_DENSITY
        kind = kind or barcodes.kind(barcode)

        if check_media:
            problem = self.check_media(library_id, density, barcode)
            if problem:
                return failure_result(f'Not creating {barcode}: {problem}',
                                      [problem], operation_id)

        try:
            with FileLock(lock_path(self.config_dir)):
                contents = self._contents(library_id)
                if contents is None:
                    return failure_result(
                        f'Could not read library {library_id}',
                        ['library_contents is not readable'], operation_id)

                if barcode in contents.barcodes:
                    return failure_result(f'{barcode} is already in library {library_id}',
                                          ['barcodes must be unique'], operation_id)

                target = self._choose_slot(contents, slot)
                if isinstance(target, ServiceResult):
                    return target

                made = media.create(barcode, library_id=library_id, size_mb=size_mb,
                                    density=density, kind=kind, base=self.media_dir)
                if not made.ok:
                    return failure_result(f'mktape could not create {barcode}',
                                          [made.output.strip()[:300]], operation_id)

                recorded = self._record(library_id, contents, target, barcode)
                if not recorded.success:
                    return recorded

            return success_result(
                f'Created {barcode} in slot {target} of library {library_id}',
                {'barcode': barcode, 'library_id': library_id, 'slot': target,
                 'density': density, 'kind': kind, 'size_mb': size_mb}, operation_id)

        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('creating tape %s', barcode)
            return failure_result(f'Error creating {barcode}: {exc}', [str(exc)],
                                  operation_id)

    def create_bulk(self, library_id: int, count: int, *, prefix: str = None,
                    suffix: str = None, start_number: int = None,
                    size_mb: int = DEFAULT_SIZE_MB,
                    density: str = None, kind: str = None) -> ServiceResult:
        """Create a numbered run of tapes.

        Stops at the first failure and reports what was made, rather than
        carrying on and leaving the operator to work out which of fifty tapes
        exist. A density with no suffix gets the density's suffix; a suffix
        with no density gets the suffix's density; the pair is checked against
        the library's drives once, before anything is made.
        """
        operation_id = str(uuid.uuid4())[:8]
        if count < 1:
            return failure_result('Count must be at least 1', ['nothing to do'],
                                  operation_id)

        kind = kind or 'data'
        if kind not in barcodes.KIND_DIGITS:
            return failure_result(f'Unknown tape kind {kind!r}',
                                  [f'use one of {", ".join(barcodes.KIND_DIGITS)}'],
                                  operation_id)

        existing = self.barcodes_in(library_id)
        prefix = prefix or barcodes.detect_prefix(existing)
        density = (density or '').upper() or None
        suffix = (suffix or '').upper() or None
        if density and not suffix:
            suffix = personalities.suffix_for(density, kind or 'data')
        suffix = suffix or barcodes.detect_suffix(existing)
        density = density or personalities.DENSITY_BY_SUFFIX.get(suffix or '') \
            or self.media_for_library(library_id)['default'] or DEFAULT_DENSITY
        if not prefix and kind != 'clean':
            return failure_result(
                f'Cannot tell what barcode series library {library_id} uses',
                ['the library has no data tapes yet; pass a prefix'], operation_id)

        head = barcodes.kind_head(kind, library_id, prefix)
        digits = barcodes.KIND_DIGITS[kind]
        if kind == 'data':
            start = start_number or barcodes.next_number(existing, head, suffix)
        else:
            start = start_number or barcodes.first_free(existing, head, digits)
        last = start + count - 1
        if last > barcodes.MAX_PER_KIND[kind]:
            return failure_result(
                f'Not enough {kind} barcodes left in library {library_id}',
                [f'{head}{"n" * digits} numbers stop at '
                 f'{barcodes.MAX_PER_KIND[kind]}; this run would reach {last}'],
                operation_id)

        try:
            wanted = barcodes.series(head, suffix, start, count, digits=digits)
        except barcodes.InvalidBarcode as exc:
            return failure_result(f'Invalid barcode series: {exc}', [str(exc)],
                                  operation_id)

        problem = self.check_media(library_id, density, wanted[0] if wanted else None)
        if problem:
            return failure_result(f'No tapes created in library {library_id}: {problem}',
                                  [problem], operation_id)

        created, failures = [], []
        for barcode in wanted:
            result = self.create(library_id, barcode, size_mb=size_mb,
                                 density=density, kind=kind, check_media=False)
            if result.success:
                created.append(result.data)
            else:
                failures.append(f'{barcode}: {result.message}')
                break

        if not created:
            return failure_result(f'No tapes created in library {library_id}',
                                  failures, operation_id)

        message = f'Created {len(created)} of {count} tapes in library {library_id}'
        result = success_result(message, {'library_id': library_id,
                                          'created': created,
                                          'requested': count,
                                          'failed': failures}, operation_id)
        if failures:
            result.success = False
            result.errors = failures
            result.message = f'{message}; stopped at {failures[0]}'
        return result

    def adopt(self, library_id: int, barcode: str, *,
              slot: int = None) -> ServiceResult:
        """Put a tape that already has media files into a library's slot.

        The counterpart of `delete` without remove_media, which leaves a tape's
        data on disk with no library listing it (libraries/orphans reports
        those). Nothing is written to the media: this only adds the barcode to
        library_contents, so the tape comes back with everything that was on
        it.

        Refuses a barcode another library already lists - two libraries
        claiming one tape means two robots moving the same data.
        """
        operation_id = str(uuid.uuid4())[:8]

        try:
            barcode = barcodes.validate((barcode or '').upper())
        except barcodes.InvalidBarcode as exc:
            return failure_result(f'Invalid barcode: {exc}', [str(exc)], operation_id)

        if not media.exists(barcode, self.media_dir):
            return failure_result(
                f'{barcode} has no media files',
                [f'nothing to adopt at {media.path_for(barcode, self.media_dir)}',
                 'use `tape create` to make a new tape'], operation_id)

        owner = self.owner_of(barcode)
        if owner is not None:
            return failure_result(
                f'{barcode} is already in library {owner}',
                ['a tape belongs to one library at a time'], operation_id)

        density = barcodes.density_for(barcode)
        problem = self.check_media(library_id, density, barcode) if density else None
        if problem:
            return failure_result(f'Not adopting {barcode}: {problem}',
                                  [problem], operation_id)

        try:
            with FileLock(lock_path(self.config_dir)):
                contents = self._contents(library_id)
                if contents is None:
                    return failure_result(
                        f'Could not read library {library_id}',
                        ['library_contents is not readable'], operation_id)

                target = self._choose_slot(contents, slot)
                if isinstance(target, ServiceResult):
                    return target

                recorded = self._record(library_id, contents, target, barcode)
                if not recorded.success:
                    return recorded
        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('adopting tape %s', barcode)
            return failure_result(f'Error adopting {barcode}: {exc}', [str(exc)],
                                  operation_id)

        return success_result(
            f'{barcode} is now in slot {target} of library {library_id}, '
            f'with its data',
            {'barcode': barcode, 'library_id': library_id, 'slot': target,
             'density': density, 'kind': barcodes.kind(barcode),
             'restart_required': True}, operation_id)

    def unassigned(self, *, with_usage: bool = True) -> ServiceResult:
        """Tapes whose files are on disk but that no library_contents lists.

        The inventory side of libraries/orphans: what `delete` without
        remove_media leaves, and what a restored media directory brings back.
        Each one can be given to a library again with adopt().
        """
        operation_id = str(uuid.uuid4())[:8]
        try:
            loose = [barcode for barcode in media.list_media(self.media_dir)
                     if self.owner_of(barcode) is None]
        except OSError as exc:                         # noqa: BLE001 - reported
            return failure_result(f'Could not read the media directory: {exc}',
                                  [str(exc)], operation_id)

        usage = media.usage_for_all(loose, self.media_dir) if (loose and with_usage) else {}
        tapes = []
        for barcode in sorted(loose):
            measured = usage.get(barcode)
            tapes.append({
                'barcode': barcode,
                'path': str(media.path_for(barcode, self.media_dir)),
                'density': barcodes.density_for(barcode),
                'kind': barcodes.kind(barcode),
                'used_mb': measured.used_mb if measured else None,
            })
        return success_result(
            f'{len(tapes)} tape(s) on disk that no library lists',
            {'tapes': tapes, 'count': len(tapes)}, operation_id)

    def owner_of(self, barcode: str):
        """The id of the library whose library_contents lists this barcode, or
        None. Every contents file in the configuration directory is read, not
        only the declared libraries: a tape in a library that is merely stopped
        still belongs to it."""
        directory = Path(self.config_dir) if self.config_dir else config_dir()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return None
        for entry in entries:
            match = CONTENTS_RE.match(entry.name)
            if not match:
                continue
            try:
                text = entry.read_text(errors='replace')
            except OSError:
                continue
            if barcode in contents_format.parse(text).barcodes:
                return int(match.group(1))
        return None

    def create_missing(self, library_id: int, *, size_mb: int = DEFAULT_SIZE_MB,
                       density: str = None) -> ServiceResult:
        """Make the media files for barcodes library_contents lists but that
        have none on disk.

        What a new library needs - its library_contents is written with the
        barcodes, the files are not - and the repair after a configuration
        restore. Each tape's density comes from its barcode suffix, then from
        `density`, then from the library's first writable medium. Barcodes that
        already have files are left alone.

        Moved from the adapter's create_tapes_from_library_contents, which
        took no density; the create-library workflow passed one, so the call
        raised TypeError and no new library got its tapes.
        """
        operation_id = str(uuid.uuid4())[:8]
        contents = self._contents(library_id)
        if contents is None:
            return failure_result(f'Could not read library {library_id}',
                                  ['library_contents is not readable'], operation_id)

        fallback = ((density or '').upper() or None
                    or self.media_for_library(library_id)['default']
                    or DEFAULT_DENSITY)
        created, failed, skipped = [], [], []
        for slot in contents.occupied:
            if media.exists(slot.barcode, self.media_dir):
                skipped.append(slot.barcode)
                continue
            try:
                barcode = barcodes.validate(slot.barcode)
            except barcodes.InvalidBarcode as exc:
                logger.warning('refusing to create media for %r: %s', slot.barcode, exc)
                failed.append(slot.barcode)
                continue
            made = media.create(barcode, library_id=library_id, size_mb=size_mb,
                                density=barcodes.density_for(barcode) or fallback,
                                kind=barcodes.kind(barcode), base=self.media_dir)
            (created if made.ok else failed).append(barcode)

        message = (f'{len(created)} tape(s) created, {len(skipped)} already present'
                   + (f', {len(failed)} failed' if failed else '')
                   + f' in library {library_id}')
        data = {'library_id': library_id, 'created': created,
                'skipped': skipped, 'failed': failed}
        if failed:
            result = failure_result(message,
                                    [f'could not create media for {b}' for b in failed],
                                    operation_id)
            result.data = data
            return result
        return success_result(message, data, operation_id)

    def delete(self, library_id: int, barcode: str, *,
               remove_media: bool = False) -> ServiceResult:
        """Remove a tape from its slot, and optionally delete its files."""
        operation_id = str(uuid.uuid4())[:8]

        try:
            barcode = barcodes.validate((barcode or '').upper())
        except barcodes.InvalidBarcode as exc:
            return failure_result(f'Invalid barcode: {exc}', [str(exc)], operation_id)

        try:
            with FileLock(lock_path(self.config_dir)):
                contents = self._contents(library_id)
                if contents is None:
                    return failure_result(f'Could not read library {library_id}',
                                          ['library_contents is not readable'],
                                          operation_id)

                slot = next((s for s in contents.occupied if s.barcode == barcode), None)
                if slot is None:
                    return failure_result(f'{barcode} is not in library {library_id}',
                                          ['nothing to remove'], operation_id)

                slot.barcode = None
                written = self._write_contents(library_id, contents)
                if not written.success:
                    return written

                removed = False
                if remove_media:
                    result = media.delete(barcode, self.media_dir)
                    removed = result.ok
                    if not removed:
                        logger.warning('could not delete media for %s: %s',
                                       barcode, result.output.strip()[:200])

            detail = ' and its media' if removed else ''
            return success_result(
                f'Removed {barcode} from slot {slot.number}{detail}',
                {'barcode': barcode, 'library_id': library_id, 'slot': slot.number,
                 'media_removed': removed}, operation_id)

        except Exception as exc:                       # noqa: BLE001 - reported
            logger.exception('deleting tape %s', barcode)
            return failure_result(f'Error deleting {barcode}: {exc}', [str(exc)],
                                  operation_id)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _choose_slot(contents, slot: Optional[int]):
        """The slot to use, or a failure explaining why the asked-for one will not do."""
        if slot is None:
            empty = next((s for s in contents.slots if not s.full), None)
            if empty is None:
                return failure_result('The library has no empty slots',
                                      ['every slot is occupied'])
            return empty.number

        target = next((s for s in contents.slots if s.number == slot), None)
        if target is None:
            return failure_result(f'Slot {slot} does not exist',
                                  [f'the library has {len(contents.slots)} slots'])
        if target.full:
            return failure_result(f'Slot {slot} already holds {target.barcode}',
                                  ['choose an empty slot'])
        return slot

    def _record(self, library_id: int, contents, slot: int, barcode: str) -> ServiceResult:
        for entry in contents.slots:
            if entry.number == slot:
                entry.barcode = barcode
                break
        return self._write_contents(library_id, contents)

    def _write_contents(self, library_id: int, contents) -> ServiceResult:
        path = library_contents_path(library_id, self.config_dir)
        text = contents_format.render(contents)
        try:
            from ..core import atomic_write_text
            atomic_write_text(path, text)
            return success_result('library_contents written')
        except PermissionError:
            result = shell.sudo_tee(path, text)
            if result.ok:
                return success_result('library_contents written through sudo')
            return failure_result(f'Could not write {path}', [result.stderr.strip()])
        except OSError as exc:
            return failure_result(f'Could not write {path}', [str(exc)])
