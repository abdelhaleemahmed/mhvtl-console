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
from ..config import settings
from ..config.service import ConfigService
from ..profiles import catalogue
from ..profiles import data as profiles_data
from ..profiles import ltfs_support, personalities
from ..core import (FileLock, ServiceResult, config_dir, failure_result,
                    library_contents_path, lock_path, shell, success_result)
from . import barcodes, ltfs_state, media, palette
from .models import TapeInfo

#: library_contents.10 -> 10, as in libraries/orphans.
CONTENTS_RE = re.compile(r'^library_contents\.(\d+)$')

logger = logging.getLogger(__name__)

#: What a density MHVTL gives no native capacity - 9840, 9940 - holds as far
#: as anything here can tell. Kept because `native_mb` still has to answer for
#: those densities; it is no longer the size anything is created at, which is
#: settings.DEFAULT_TAPE_SIZE_MB and happens to be the same number.
UNKNOWN_SIZE_MB = 1000

DEFAULT_DENSITY = 'LTO8'


def native_mb(density: str) -> int:
    """What a cartridge of this density really holds.

    Its native capacity, from ``personalities.NATIVE_CAPACITY_GB`` - which is
    transcribed from MHVTL and checked against it. An LTO-8 holds 12 TB and a
    T10KC 5 TB.

    **This is not the size a new cartridge is made at.** It answers a question
    about the hardware, and ``size_for`` answers a question about this host.
    They were the same function until 6 October 2026, which is why the console
    made 12 TB cartridges on a system whose purpose is testing - see
    ``size_for`` and docs/sphinx/guides/plan-tape-size.rst.

    It is what the creation forms offer as *native*, beside the size they are
    pre-filled with, so an operator can see both and pick.
    """
    capacity = personalities.NATIVE_CAPACITY_GB.get((density or '').upper())
    return capacity * 1000 if capacity else UNKNOWN_SIZE_MB


#: The old name. `native_size_mb` read as "the size", and it was the size, and
#: that was the bug: a cartridge nobody can fill in less than 63 hours on a
#: system that exists to be filled. Kept as an alias so nothing breaks while
#: the callers move; it answers what the hardware holds, like native_mb.
native_size_mb = native_mb


def size_for(density: str, base=None, data=None) -> int:
    """How big a new cartridge of this density is made.

    The chain, each level narrower than the last:

        1. ``settings.DEFAULT_TAPE_SIZE_MB``   1,000 MB, in the code
        2. ``tape.size.default``               the settings file, every density
        3. ``tape.size.<density>``             the settings file, one density

    A fourth level - ``--size-mb``, or the number in the form - is applied by
    the callers, which pass it instead of asking here.

    **Not the native capacity.** An LTO-8 holds 12 TB and is created at 1 GB,
    because the files are sparse so the size costs no disk, but it costs time:
    at the 55 MB/s this host writes, filling a native LTO-8 takes 63 hours. End
    of tape, multi-volume spanning and the fullness bar are the things a
    virtual library exists to exercise, and none of them is reachable at 12 TB.
    Anyone who wants a realistic cartridge says so in the settings file, where
    the native capacity is written beside each density as the reference.

    ``data`` is an already-read settings file, for a caller answering this for
    every density at once; without it the file is read here.
    """
    return settings.tape_size_mb(density, base, data=data)


def media_label(density: str, base=None, data=None) -> str:
    """A cartridge as a drop-down option: ``'LTO8 (1 GB)'``.

    The size is what one will be made at - ``size_for``, the same call the
    creation does - so what is chosen and what arrives cannot differ. Set
    ``tape.size.LTO8`` to 12TB and this option reads ``LTO8 (12 TB)``.

    IT USED TO READ THE NATIVE CAPACITY
    -----------------------------------
    ``personalities.media_label`` built this string from
    ``NATIVE_CAPACITY_GB``, and that was right while a cartridge was created
    at its native capacity: the option named what you would get. Once the two
    became separate questions the option went on saying ``LTO8 (12 TB)`` about
    a tape that would be made at 1 GB - wrong by a factor of twelve thousand,
    in the one place the operator is choosing.

    So the label lives here and not in ``profiles``, which cannot see the
    settings file: ``profiles`` holds facts about hardware, and how big this
    host makes a cartridge is not one. The native capacity has not gone
    anywhere - ``native_mb`` answers for it, the Settings page and
    ``mhvtl settings list`` show it in their own column, and the help line
    under the size field names it as the figure to type for a full-size tape.
    """
    density = (density or '').upper()
    if not density:
        return ''
    size = size_for(density, base, data=data)
    return f'{density} ({settings.as_short_size(size)})'


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

    def list(self, library_id: int, *, with_usage: bool = True,
             with_ltfs: bool = False) -> ServiceResult:
        """Every tape in a library.

        with_usage=False skips the disk measurement entirely, for callers that
        only need to know what is in which slot.

        with_ltfs is OFF by default, and that is the point. Reading whether each
        cartridge is an LTFS volume means reading each cartridge's own memory,
        and most callers never show the answer - a backup application wants a
        tape device, not a filesystem. Only the two callers that display it ask:
        the library detail page's chips and `mhvtl ltfs tapes`.

        It needs with_usage, because the partition count comes from that pass, so
        asking for LTFS turns usage on rather than silently returning nothing.

        Even when asked, two gates come first and neither touches a cartridge:

            1. does any drive in this library open LTFS at all? If not, the
               whole library is answered here - see _ltfs_possible_here()
            2. is the cartridge a generation that can be partitioned?
               ltfs_state.could_hold_ltfs(), from the barcode

        What is skipped reports NOT_ASKED, never PLAIN. A cartridge nobody read
        may still be an LTFS volume written elsewhere.
        """
        operation_id = str(uuid.uuid4())[:8]
        if with_ltfs:
            with_usage = True
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
            # A second pass for the LTFS state, only when a caller asked for it
            # and only where it is possible: one tar for the whole library, the
            # same shape as the first. The partition count comes from the usage
            # pass, which already counted the data.N files.
            states = (ltfs_state.state_for_all(
                [t.barcode for t in tapes],
                {b: u.partitions for b, u in usage.items()},
                self.media_dir)
                if with_ltfs and self._ltfs_possible_here(library_id) else {})
            for tape in tapes:
                found = usage.get(tape.barcode)
                if found:
                    tape.used_mb = found.used_mb
                    tape.capacity_mb = found.capacity_mb
                    tape.media_exists = found.exists
                    tape.layout = found.layout
                    tape.partitions = found.partitions
                state = states.get(tape.barcode)
                if state:
                    tape.ltfs_state = state.state
                    tape.ltfs_was = state.was_ltfs

        return success_result(
            f'{len(tapes)} tape{"s" if len(tapes) != 1 else ""} in library {library_id}',
            {'library_id': library_id, 'count': len(tapes),
             'tapes': [tape.to_dict() for tape in tapes],
             'summary': contents.summary()}, operation_id)

    def _ltfs_possible_here(self, library_id: int) -> bool:
        """Can any drive in this library open LTFS?

        The cheapest of the gates and the one that saves the most: it reads
        device.conf, which is a file, and asks a table. No cartridge, no media
        directory, no privilege beyond the configuration read. A library with no
        LTFS-capable drive is answered in one step however many tapes it holds -
        on this host that is every SONY and StorageTek library, because LTFS has
        no table for either vendor id.

        LAYERING: the drives come from ConfigService and the verdict from
        profiles/ltfs_support, both of which this module already imports.
        `operations.mounting.library_drives()` would read better and cannot be
        used: operations/ imports tapes/, so tapes/ -> operations/ would close a
        cycle. See rule 7 in services/__init__.py.
        """
        conf = ConfigService(self.config_dir).device_conf()
        if conf is None:
            # Cannot tell, so do not filter. A caller that asked for LTFS state
            # gets the read rather than a silent "no".
            return True
        for _, drive in sorted(conf.drives_of(int(library_id)).items()):
            if ltfs_support.supports(drive.get('vendor', ''),
                                     drive.get('product', ''),
                                     drive.get('revision') or None).supported:
                return True
        return False

    def palette(self) -> ServiceResult:
        """Every generation, its real shell colour and how it is drawn.

        Reference data, no library and no hardware: the CLI prints it as a
        legend and the web renders the same tokens through the stylesheet.
        """
        rows = palette.rows()
        return success_result(
            f'{len(rows) - 1} LTO generations, and one entry for media that is '
            f'not LTO at all',
            data={'palette': rows,
                  'collisions': palette.collisions()},
            operation_id='tape_palette')

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

    def libraries_for(self, density: str) -> ServiceResult:
        """The libraries a tape of this density could go into.

        ``media_for_library`` asked the other way round, for a caller that
        has a tape and needs the libraries rather than a library and needs
        the tapes. The console's adopt form and ``tape adopt``'s refusal both
        use it, so neither decides for itself what a drive can load.

        LOADS, NOT WRITES
        -----------------
        An LTO-5 cartridge belongs in a library whose newest drive is an
        LTO-7: that drive reads LTO-5, and adopting recovers what is on a
        tape rather than writing to it. ``check_media`` draws the line in the
        same place, which is what lets this be offered as the choice that is
        enforced.

        A library whose drives MHVTL gives no media list for is offered, for
        the same reason ``check_media`` lets it through: it is not known to
        be wrong.
        """
        operation_id = str(uuid.uuid4())[:8]
        conf = ConfigService(self.config_dir).device_conf()
        if conf is None:
            return failure_result(
                'Could not read the library configuration',
                ['device.conf is not readable'], operation_id)

        able, unable = [], []
        for library_id in sorted(conf.libraries):
            declared = conf.libraries[library_id]
            takes = self.media_for_library(library_id)
            offered = [entry['density'] for entry in takes['media']]
            if not takes['known'] or density in offered:
                able.append({'library_id': library_id,
                             'vendor': declared.get('vendor', ''),
                             'product': declared.get('product', ''),
                             'takes': offered})
            else:
                unable.append({'library_id': library_id, 'takes': offered})

        if able:
            # No article before the density: "a LTO5" and "an AIT3" would
            # need a rule about vowels to say nothing.
            says = (f'{len(able)} librar{"ies" if len(able) != 1 else "y"} '
                    f'can take {density}')
        elif unable:
            says = (f'No library on this host has a drive that loads {density}. '
                    f'Add one with `mhvtl drive add <library> --model <a drive '
                    f'that takes {density}>`, or make a library that does.')
        else:
            says = 'There are no libraries on this host yet.'
        return success_result(says,
                              {'libraries': able, 'cannot': unable,
                               'density': density, 'says': says}, operation_id)

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
        # The default is the same rule creation applies before the library
        # exists, so a mixed library's nominal density is one answer rather
        # than two: profiles/catalogue.default_density_for.
        return {'library_id': library_id, 'drives': drives,
                'known': bool(media_list), 'media': media_list,
                'default': catalogue.default_density_for(drives)}

    def media_context(self, libraries) -> Dict:
        """Everything a creation form needs to offer only usable media.

        Per library, the densities its drives load and which are read-only;
        plus, for every density that can be created, its barcode suffixes, its
        label, **how big one will be made** and **what one really holds**.

        WHY THOSE LAST TWO ARE SEPARATE KEYS
        ------------------------------------
        ``default_mb`` is what the field is pre-filled with - the answer
        ``size_for`` gives, which is the answer a create with no size actually
        produces. ``native_mb`` is what the cartridge holds, offered beside it
        so an operator can choose a realistic capacity knowingly.

        They were one number until 6 October 2026, derived in the view as
        ``gb * 1000``: the form suggested the native capacity and the service
        created the native capacity, so they agreed by coincidence. The moment
        the two stopped being the same question, a page that computed its own
        suggestion would have gone on filling in 12 TB while ``mhvtl`` made
        1 GB - a tape's size depending on which front end you used, which is
        the bug 3.2.0 was written to end.

        So the view asks for this and renders it. It derives nothing, and the
        command line can ask the same question.
        """
        library_media = {}
        for lib in libraries:
            library_id = lib['library_id'] if isinstance(lib, dict) else lib
            try:
                library_media[library_id] = self.media_for_library(library_id)
            except Exception:                 # noqa: BLE001 - then offer all
                logger.exception('reading the drives of library %s', library_id)

        densities = list(personalities.SUFFIX_BY_DENSITY)

        # Grouped by family, in the order the catalogue lists them, so the
        # drop-down reads LTO / AIT / DLT with the generations under each
        # rather than thirty-two flat entries. The page renders these as
        # optgroups and decides nothing: which family a cartridge belongs to
        # is profiles.personalities.media_family.
        # The settings file read once for the whole context, not once per
        # density per key: the labels and the sizes are the same question
        # asked twice about thirty-three cartridges, and a page assembled
        # from sixty-six readings is a page that can be built half from
        # before somebody's edit and half from after it.
        saved = settings.read()
        labels = {d: media_label(d, data=saved) for d in densities}
        sizes = {d: size_for(d, data=saved) for d in densities}

        families: Dict[str, List] = {}
        for density in densities:
            families.setdefault(personalities.media_family(density), []).append(
                (density, labels[density]))

        return {
            'densities': [(d, labels[d]) for d in densities],
            'density_families': list(families.items()),
            'media_info': {
                'libraries': library_media,
                'suffix': personalities.SUFFIX_BY_DENSITY,
                'worm_suffix': personalities.WORM_SUFFIX_BY_DENSITY,
                'labels': labels,
                'default_mb': sizes,
                'native_mb': {d: native_mb(d) for d in densities},
                'unknown_size_mb': UNKNOWN_SIZE_MB,
            },
        }

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
               size_mb: int = None, density: str = None,
               kind: str = None, check_media: bool = True) -> ServiceResult:
        """Create one tape and put it in a slot.

        The density defaults to what the barcode's suffix says, then to the
        library's first writable medium. check_media=False skips the drive
        check, for create_bulk, which has already made it once for the run.

        ``size_mb`` of None means the density's native capacity, which is
        why it is resolved after the density and not in the signature - see
        native_size_mb.
        """
        operation_id = str(uuid.uuid4())[:8]

        try:
            barcode = barcodes.validate((barcode or '').upper())
        except barcodes.InvalidBarcode as exc:
            return failure_result(f'Invalid barcode: {exc}', [str(exc)], operation_id)

        density = (density or '').upper() or barcodes.density_for(barcode) \
            or self.media_for_library(library_id)['default'] or DEFAULT_DENSITY
        kind = kind or barcodes.kind(barcode)
        if size_mb is None:
            size_mb = size_for(density)

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
                    size_mb: int = None,
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

    def create_missing(self, library_id: int, *, size_mb: int = None,
                       density: str = None,
                       sizes: Dict[str, int] = None) -> ServiceResult:
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

        HOW BIG EACH ONE IS
        -------------------
        A library can hold more than one kind of cartridge, so one number for
        the run is the wrong shape: an LTO-8 and a DLT-4 made by the same
        create are different sizes. ``sizes`` is ``{density: mb}`` - what the
        creation wizard asked for, per kind - and a density it does not name
        falls through to ``size_for``, which is the settings file and then the
        shipped default.

        ``size_mb`` is the older, blunter answer: one size for every cartridge
        in the run, whatever its density. It still wins where it is given,
        because `tape bulk --size-mb` means exactly that.
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
            # Per tape, not per run: the density comes from each barcode, so
            # the capacity has to as well. A library holding LTO-8 and DLT-4
            # is two sizes, and asking once for the run would give the second
            # kind the first kind's capacity.
            #
            # One size for everything, then the size asked for this kind, then
            # the chain - settings file, then the shipped default.
            for_tape = barcodes.density_for(barcode) or fallback
            this_one = (size_mb
                        or (sizes or {}).get((for_tape or '').upper())
                        or size_for(for_tape))
            made = media.create(barcode, library_id=library_id,
                                size_mb=this_one,
                                density=for_tape,
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
