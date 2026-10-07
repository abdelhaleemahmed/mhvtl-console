"""The create-a-library form, decided here rather than in the browser.

One call answers the whole page: which options each dropdown holds, **which
one is selected**, how many drives and cartridges this host can actually give
it, and the sentences that explain all of it. The page renders that answer and
decides nothing; the AJAX endpoint behind every change calls the same
function, so a changed dropdown and a freshly opened page are answered by one
piece of code.

WHY IT EXISTS
-------------
``brand_config.html`` narrowed its own dropdowns in JavaScript and chose its
own selected option, which is the rule this project follows nowhere else - and
it drifted from the services three times:

- it filtered densities on what a drive LOADS where the services mean what it
  can WRITE, so the form offered a library that restores and never backs up;
- it chose the last writer listed, which answered a half-height ULT3580-HH9
  for LTO-9 where the services answer the full-height TD9;
- ``applyLimits()`` reset the drive count on every model change, so a preset's
  ``value="2"`` was rendered into the HTML and overwritten before anyone saw
  4 - and the Django test asserting ``value="2"`` passed the whole time,
  because it sees the rendered attribute and not what the script does to the
  DOM.

Three things the page did BETTER than the terminal, and they are all here now:
the drive count is capped by the host's free SCSI targets rather than by the
model's element layout alone; the serial number looks like a device's
(``profiles/personalities.device_serial``); and the limits travel with the
answer, so the page keeps its immediate feedback without asking the server per
keystroke.

NOTHING HERE IS A NEW RULE
--------------------------
Every answer is composed from something that already decided it::

    catalogue.describe            which models take the wanted tape, which
                                  drives each model takes, which drive a model
                                  gets - narrowed by one argument
    catalogue.densities_of        the tapes a vendor's drives take, in
                                  generation order
    tapes.media_label             'LTO8 (1 GB)' - the size a cartridge of
                                  this kind will be made at, so the option
                                  names what choosing it produces
    config.ids                    the SCSI target and device-id budgets
    spec.apply_defaults           the serial, the barcode prefix, the suffix,
                                  the revision - so the form shows exactly
                                  what creation will write
    profiles.data                 the counts a profile defaults to

So this module composes and formats. If an answer here is wrong, it is wrong
in the service that owns it, and the terminal is wrong in the same way - which
is the whole point.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
from typing import Any, Dict, List, Optional

from ..config import device_conf as device_conf_format
from ..config import ids
from ..core import ServiceResult, device_conf_path, failure_result, shell, \
    success_result
from ..profiles import catalogue, personalities
from ..profiles.data import get_media_suffix, get_profile_options
from . import spec as spec_rules

logger = logging.getLogger(__name__)

#: The library id to offer when device.conf has no room left for another. The
#: form cannot create one in that state and the limits say so; this keeps the
#: page renderable rather than failing before it can explain itself.
FALLBACK_LIBRARY_ID = ids.LIBRARY_STEP


def state(profile_key: str, *, library_id: int = None,
          wanted_media: str = None, library_model: str = None,
          drive_runs=None, media_runs=None, add: str = None,
          drive_model: str = None, media_type: str = None,
          num_drives=None, media_count=None, empty_slots=None,
          preset: Dict[str, Any] = None,
          config_dir=None) -> ServiceResult:
    """The whole form, for these choices: options, selections, limits, words.

    Every argument is a choice already made - by a dropdown the operator
    changed, by ``?media=`` carried over from the vendor page, or by a preset
    the caller resolved. Anything not given is answered from the catalogue,
    and anything given that this vendor cannot do is ignored rather than
    refused, so a stale link or an old preset opens the page instead of
    failing on it.

    ``preset`` is the specification, not a name: the caller resolves it with
    ``libraries.presets.resolve`` and reports its own messages, which keeps
    this function free of file I/O and keeps the web's "starting from the
    saved configuration 'x'" message where the web can see it.

    MORE THAN ONE KIND
    ------------------
    ``drive_runs`` and ``media_runs`` are the rows: ``['ULT3580-TD8:2',
    'ULT3580-TD6:2']``, the same ``MODEL:COUNT`` the ``--drive`` flag takes,
    or the parsed list. ``drive_model``/``num_drives`` remain for a caller
    that has only ever asked for one kind, and collapse to a single row.

    A preset's own ``[[name.drive]]`` list fills the rows, which is a defect
    fixed by construction: the form had one dropdown for each, so a preset
    holding ``2 x TD8 + 2 x TD6`` and ``20 x LTO8 + 10 x LTO6`` opened as
    ``TD8`` and ``LTO8`` with the vendor's default counts - 4 drives and 50
    cartridges where the preset said 4 and 30 - under a message saying the
    preset had been applied.
    """
    try:
        key = catalogue.resolve(profile_key)
    except catalogue.UnknownProfile as unknown:
        return failure_result(str(unknown), unknown.fixes)

    preset = dict(preset or {})
    options = get_profile_options(key)
    defaults = options['defaults']

    # What device.conf makes possible, here and now. Read the way
    # validation.validate reads it, and an unreadable file is an empty one:
    # the first library on a host is created before the file exists.
    existing = shell.sudo_cat(device_conf_path(config_dir))
    conf = device_conf_format.parse(existing.stdout if existing.ok else '')
    if library_id is None:
        library_id = ids.next_library_id(conf) or FALLBACK_LIBRARY_ID

    # The tape the operator asked for narrows everything after it, so it is
    # settled first. A preset's own density counts as asking for it.
    wanted = _offered(wanted_media or preset.get('media_type'),
                      catalogue.densities_of(options['drive_models']))
    described = catalogue.describe(key, media=wanted or None)

    models = [row['model'] for row in described['models']]
    model = _choose(models, library_model, preset.get('library_model'),
                    defaults['library_model'])
    for_model = next((row for row in described['models']
                      if row['model'] == model), {})
    limits = _limits(for_model, conf, library_id)

    # The drives, one row per kind. A library may hold two generations at
    # once, so what arrives is a list and not a choice: from the form's rows,
    # from a preset's [[name.drive]] array, or - for a caller that has only
    # ever asked for one - from the single model and count.
    try:
        asked_drives = _asked_runs(
            drive_runs, 'model',
            preset.get('drive'),
            single=(drive_model or preset.get('drive_model'),
                    num_drives if num_drives is not None
                    else preset.get('num_drives')),
            fallback=(for_model.get('default_drive'),
                      defaults['num_drives']))
        asked_media = _asked_runs(
            media_runs, 'density',
            preset.get('media'),
            single=(media_type or preset.get('media_type') or wanted,
                    media_count if media_count is not None
                    else preset.get('media_count')),
            fallback=(None, defaults['media_count']),
            # A cartridge row the operator has just added holds none yet.
            least=0)
    except ValueError as unreadable:
        return failure_result(f'that is not a kind and a count: {unreadable}',
                              ['the form is MODEL:COUNT, as in '
                               'ULT3580-TD8:2'])

    # "Add another kind" is an empty row, and what it may hold is a question
    # only the catalogue can answer: whatever the rows above have not taken.
    # The page asks for one rather than inventing it, which is why this is a
    # word and not a half-written row - `drive=:1` is a run that names
    # nothing, and parse_runs is right to refuse it.
    if add == 'drive':
        asked_drives = list(asked_drives) + [(None, 1)]
    elif add == 'media':
        asked_media = list(asked_media) + [(None, 0)]

    drives = _drive_rows(asked_drives, for_model, described, limits)
    chosen = [row['selected'] for row in drives['rows'] if row['selected']]
    media = _media_rows(asked_media, chosen, described, options, wanted,
                        limits)

    empty = _whole(empty_slots, preset.get('empty_slots'),
                   defaults['empty_slots'])
    counts = _totals(drives, media, empty, limits)

    # What creation would really write, asked of the function that writes it,
    # so the serial, the barcode and the nominal density on the form are the
    # ones that will exist - and for a mixed library the nominal density is a
    # rule of its own (the first its drives can write), which is exactly why
    # this asks rather than picking.
    filled = _filled(key, library_id, model, drives, media, counts)

    return success_result(
        f'{key} library form', {
            'profile': key,
            'library_id': library_id,
            'serial': filled.get('serial'),
            'product': filled.get('product'),
            'library_revision': filled.get('library_revision'),
            'barcode_prefix': filled.get('barcode_prefix'),
            'media_suffix': filled.get('media_suffix'),
            'media_type': filled.get('media_type'),
            'wanted_media': _select(
                catalogue.densities_of(options['drive_models']), wanted,
                label=_tapes().media_label,
                empty='Any tape this vendor supports'),
            'library_model': _select(models, model),
            'drives': drives,
            'media': media,
            'counts': counts,
            'tape_size': _tape_size(media),
            'limits': limits,
            'says': _says(model, drives, media, limits, filled, counts),
        })


def _offered(wanted: Optional[str], densities: List[str]) -> str:
    """The wanted tape, when this vendor's drives take it at all.

    Ignored rather than refused: ``?media=`` comes from the vendor page's
    filter links, and a stale one should open the page.
    """
    wanted = (wanted or '').strip().upper()
    return wanted if wanted in [density.upper() for density in densities] else ''


def _asked_runs(given, what: str, from_preset, *, single, fallback, least=1):
    """[(name, count), ...] - the kinds asked for, however they were asked.

    Four sources, in the order of authority the rest of this module uses:
    the rows the page just sent, then a preset's own list, then a single
    kind and count - which is what every caller sent before the form could
    hold more than one - and finally the catalogue's default.

    The single-kind path is why a mixed preset used to lose half of itself:
    ``preset.get('num_drives')`` is **absent** on a preset whose lists imply
    it, so the count fell through to the profile's default and the page
    showed 50 cartridges for a preset asking for 30.

    Raises ValueError when a row cannot be read, which state() turns into a
    refusal naming the form.
    """
    if given:
        runs = (given if given and isinstance(given[0], dict)
                else spec_rules.parse_runs(given, what, least=least))
        return [(run[what], int(run['count'])) for run in runs]
    if from_preset:
        return [(run[what], int(run['count'])) for run in from_preset]
    name, count = single
    if name:
        return [(name, _whole(count, fallback[1]))]
    return [(fallback[0], _whole(count, fallback[1]))]


def _drive_rows(asked, for_model: Dict, described: Dict,
                limits: Dict) -> Dict:
    """One row per kind of drive: what it may be, what it is, how many.

    A kind already chosen is not offered again in another row - the rule the
    interactive loop applies, and the reason it exists: ``--drive X:2 --drive
    X:3`` is five drives of X written confusingly. A row keeps its own
    selection in its own options, so changing another row cannot make this
    one unreadable.

    A count comes back capped against what the rows above it left, rather
    than refused, which is why the page may send what was typed on every
    change: the answer says what will be used and the input shows it. It has
    to be capped *per row*, because the rows are what creation reads - a
    total capped on its own would have shown 71 under an input saying 900 and
    then failed validation on submit.
    """
    offered = list(for_model.get('drives') or [])
    budget = limits.get('max_drives') or 0
    rows, taken, spent = [], [], 0
    for name, count in asked:
        mine = [model for model in offered
                if model not in taken or model == name]
        chosen = _choose(mine, name, for_model.get('default_drive'))
        if chosen:
            taken.append(chosen)
        count = max(int(count), 1)
        if budget:
            count = max(min(count, budget - spent), 1)
        spent += count
        rows.append({
            'options': _select(mine, chosen)['options'],
            'selected': chosen,
            'count': count,
            'can_remove': len(asked) > 1,
            # Per row, because each kind is emulated as something different
            # and writes something different. One sentence for the whole
            # section would have to leave that out.
            'says': _drive_sentence(chosen, described),
        })
    left = [model for model in offered if model not in taken]
    room = budget - spent
    return {'rows': rows,
            'can_add': bool(left) and room > 0,
            'why_not': ('' if left and room > 0 else
                        'every drive model this library takes is already '
                        'listed' if not left else
                        f"there is no room for another: "
                        f"{limits.get('max_drives')} drive(s) is the most "
                        f"this library can have, limited by "
                        f"{limits.get('bound_by')}")}


def _drive_sentence(drive: Optional[str], described: Dict) -> str:
    """'ULT3580-TD8 is emulated as init_ult3580_td8; it writes LTO8, LTO7.'

    What MHVTL will do with this choice, said before anything is written. A
    model MHVTL does not recognise is emulated as a generic drive, which is
    a thing worth knowing at the moment of choosing it rather than after.
    """
    row = next((entry for entry in described.get('drives') or []
                if entry['model'] == drive), None)
    if row is None:
        return ''
    said = (f"{drive} is emulated as "
            f"{row.get('personality') or 'a generic drive'}; "
            f"it writes {', '.join(row.get('writes') or []) or 'nothing'}")
    if row.get('reads_only'):
        said += f" and only reads {', '.join(row['reads_only'])}"
    return said + '.'


def _media_rows(asked, drives: List[str], described: Dict, options: Dict,
                wanted: str, limits: Dict) -> Dict:
    """One row per kind of cartridge, offering what these drives take.

    Loads, not writes: a library that only restores is a real thing to build
    and MHVTL allows it, so a generation a drive can only read is offered
    and **labelled**. Which is also why the options depend on every drive
    chosen, not on the first one: an LTO-6 cartridge belongs in a library
    that has a TD6, even though the TD8 beside it cannot read it.
    """
    loads, read_only = [], []
    for row in described.get('drives') or []:
        if row['model'] not in drives:
            continue
        for density in row.get('loads') or []:
            if density not in loads:
                loads.append(density)
        for density in row.get('reads_only') or []:
            if density not in read_only:
                read_only.append(density)
    # Only read-only where NO chosen drive writes it: a density one drive
    # reads and another writes is simply writable in this library.
    writable = {density for row in described.get('drives') or []
                if row['model'] in drives
                for density in row.get('writes') or []}
    read_only = [density for density in read_only if density not in writable]

    native = options['default_media_by_drive'].get(drives[0]) if drives else None
    budget = limits.get('max_slots') or 0
    rows, taken, spent = [], [], 0
    for name, count in asked:
        mine = [density for density in loads
                if density not in taken or density == name]
        chosen = _choose(mine, name, wanted, native)
        if chosen:
            taken.append(chosen)
        # Capped against the slots the rows above left, for the reason
        # _drive_rows is: the rows are what creation reads. Cartridges come
        # before empty slots, so this caps against the layout's whole slot
        # count and _totals gives the empty ones what is left.
        count = max(int(count), 0)
        if budget:
            count = max(min(count, budget - spent), 0)
        spent += count
        # How big a cartridge of this kind will be, asked of the service that
        # will make it. The row carries its own, because a library holding
        # LTO-8 and DLT-4 is holding two capacities.
        size_mb = _tapes().size_for(chosen or '')
        rows.append({
            'options': _select(mine, chosen,
                               label=lambda value: _media_label(
                                   value, drives, read_only))['options'],
            'selected': chosen,
            'count': count,
            'read_only': chosen in read_only,
            'can_remove': len(asked) > 1,
            'size_mb': size_mb,
            'size_shown': _sizes().as_size(size_mb),
        })
    left = [density for density in loads if density not in taken]
    return {'rows': rows, 'can_add': bool(left),
            'why_not': '' if left else
            'every tape these drives take is already listed'}


def _totals(drives: Dict, media: Dict, empty: int, limits: Dict) -> Dict:
    """How many drives and cartridges the rows add up to.

    The totals are what validation and the layout care about, and what the
    page could never show for a mixed preset: lib-ten is 20 LTO8 and 10 LTO6
    and nothing said "30".

    A sum, not a second opinion: the row builders cap their own counts
    against the budget, because the rows are what creation reads. The one
    number settled here is the empty slots, which get whatever the cartridges
    left - the order the old ``_counts`` used, kept.
    """
    num_drives = sum(row['count'] for row in drives['rows'])
    media_count = sum(row['count'] for row in media['rows'])
    if limits.get('max_slots'):
        empty = min(empty, max(limits['max_slots'] - media_count, 0))
    return {'num_drives': num_drives, 'media_count': media_count,
            'empty_slots': max(int(empty), 0),
            'total_slots': media_count + max(int(empty), 0)}


def _tapes():
    """services.tapes, imported late.

    This module is imported while the parser is built, and tapes pulls in the
    whole catalogue; deferring it keeps a `--help` cheap. The same reason
    commands/*.py import their services inside their handlers.
    """
    from ..tapes import service as tapes
    return tapes


def _sizes():
    from ..config import settings as sizes
    return sizes


def _tape_size(media: Dict) -> Dict:
    """What a cartridge of the chosen density will be made at.

    So the form can show it as the field's placeholder instead of leaving a
    blank to fill in. **Asked of the service, never worked out here**: a page
    showing one number while a create produces another is the bug
    plan-tape-size exists to prevent, and it hid for months the last time
    because the view's arithmetic and the service's agreed by coincidence.

    Its own key rather than another entry in `counts`, which is the three
    numbers the form adds up and has a test pinning its shape. A capacity is
    not a count.
    """
    chosen = next((row['selected'] for row in media['rows']
                   if row.get('count') and row.get('selected')), None)
    size_mb = _tapes().size_for(chosen or '')
    return {'density': chosen, 'mb': size_mb,
            'shown': _sizes().as_size(size_mb)}


def _choose(options: List[str], *candidates) -> Optional[str]:
    """The first candidate these options actually hold, else the first option.

    The order of the candidates is the order of authority: what the operator
    just chose, then what a preset fixed, then what the catalogue defaults to.
    Narrowing happens before this - ``catalogue.describe(media=...)`` returns
    only the models and drives that take the wanted tape - so a candidate is
    dropped here exactly when the choice before it ruled the candidate out.

    The last resort is the FIRST option, not the last. The page took
    ``validDrives[validDrives.length - 1]``, and the comment above that line
    said why it was wrong: STK's L700 list ends with a DLT7000.
    """
    for candidate in candidates:
        if candidate and candidate in options:
            return candidate
    return options[0] if options else None


def _select(options: List[str], selected: Optional[str], *, label=None,
            empty: str = None) -> Dict:
    """One dropdown: its options, each marked or not, and the one chosen.

    ``[{'value', 'label', 'selected'}]`` - everything the template needs for
    an ``<option>`` and nothing it has to work out. The page built these in
    JavaScript and marked the selection itself, which is the half that drifted.

    ``empty`` adds the "no choice" option and its wording, for the one
    dropdown where no choice is a real answer: *any* tape this vendor
    supports. Given here rather than written into the template so that every
    dropdown has exactly one option marked, which is a property a test can
    check - and so that the template holds no words of its own.
    """
    offered = []
    if empty is not None:
        offered.append({'value': '', 'label': empty, 'selected': not selected})
    offered += [{'value': value,
                 'label': label(value) if label else value,
                 'selected': value == selected}
                for value in options]
    return {'selected': selected, 'options': offered}


def _media_label(density: str, drives: List[str],
                 read_only: List[str]) -> str:
    """'LTO8 (suffix: L8)', and a word when nothing here can write it.

    The suffix is in the label because MHVTL reads the density out of the
    barcode suffix and nothing else, so it is the one part of a cartridge's
    name an operator has to recognise later.

    "In this library", not "in this drive": with more than one kind of drive
    a density only one of them writes is writable here, and only a density
    *none* of them writes is read-only.
    """
    try:
        suffix = get_media_suffix(density)
    except ValueError:
        suffix = None
    label = f'{density} (suffix: {suffix})' if suffix else density
    if density in read_only:
        label += (' - read-only in this drive' if len(drives) == 1
                  else ' - read-only in every drive here')
    return label


def _limits(for_model: Dict, conf, library_id: int) -> Dict:
    """How many drives and slots this library may really have, and why.

    Three budgets apply at once and the smallest wins:

        the model's element layout   what MHVTL's personality for this model
                                     can address (profiles/personalities)
        free SCSI targets            one for the library, one per drive. This
                                     is usually the binding one, and it is
                                     the limit the terminal never had
        free device ids              1-1023, excluding the multiples of ten
                                     reserved for libraries

    ``bound_by`` names the one that decided, because "at most 71" with no
    reason reads as an arbitrary number - and because the answer changes as
    other libraries come and go.
    """
    layout_max = for_model.get('max_drives')
    free_targets = ids.free_targets(conf)
    free_ids = ids.free_drive_ids(conf, reserved=[library_id])

    budgets = [('the model', layout_max),
               ('SCSI targets', max(free_targets - 1, 0)),
               ('device ids', free_ids)]
    offered = [(name, value) for name, value in budgets if value is not None]
    bound_by, max_drives = min(offered, key=lambda budget: budget[1])

    return {
        'max_drives': max_drives,
        'max_slots': for_model.get('max_slots'),
        'max_maps': for_model.get('max_maps'),
        'layout': for_model.get('layout'),
        'layout_max_drives': layout_max,
        'free_targets': free_targets,
        'free_drive_ids': free_ids,
        'bound_by': bound_by,
    }


def _whole(*candidates) -> int:
    """The first candidate that is a whole number. The last is the default."""
    for candidate in candidates:
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return 0


def as_spec(key: str, library_id: Optional[int], model: Optional[str],
            drives: Dict, media: Dict, counts: Dict) -> Dict:
    """These rows as a specification, in the shape creation takes.

    The lists, not the counts: ``drive`` and ``media`` are what carry two
    kinds, and the counts they imply are left out, because a count beside a
    list is refused - by the file format, by the command line and by
    validation. One row collapses to the same thing a single choice always
    produced, so nothing downstream has to ask which arrived.

    Public because the view posts it: the page submits its rows and the
    specification it builds must be the one the form was showing, not a
    second reading of the same inputs.
    """
    asked = {'profile': key, 'empty_slots': counts['empty_slots']}
    if library_id is not None:
        asked['library_id'] = library_id
    if model:
        asked['library_model'] = model
        asked['product'] = model
    chosen = [row for row in drives['rows'] if row['selected']]
    if chosen:
        asked['drive'] = [{'model': row['selected'], 'count': row['count']}
                          for row in chosen]
    held = [row for row in media['rows'] if row['selected'] and row['count']]
    if held:
        asked['media'] = [{'density': row['selected'], 'count': row['count']}
                          for row in held]
    else:
        asked['media_count'] = 0
    return asked


def _filled(key: str, library_id: int, model: Optional[str],
            drives: Dict, media: Dict, counts: Dict) -> Dict:
    """What ``spec.apply_defaults`` makes of these rows.

    Asked rather than repeated: the serial, the product revision, the barcode
    prefix, the media suffix and - for a mixed library - the *nominal
    density*, the first its drives can write, are each a rule of their own. A
    form that composed them again would be a second answer, which is exactly
    what the serial was, in JavaScript, for every library created in a
    browser.
    """
    try:
        return spec_rules.apply_defaults(
            as_spec(key, library_id, model, drives, media, counts))
    except spec_rules.UnknownProfile:
        # The profile resolved at the top of state(), so this cannot be a
        # bad vendor; it is a specification too empty to fill in - a vendor
        # whose catalogue has no models, which no profile has.
        return {}


def _says(model: Optional[str], drives: Dict, media: Dict, limits: Dict,
          filled: Dict, counts: Dict) -> Dict:
    """The finished sentences, so the page concatenates nothing.

    ``services decide and format`` - the page had these composed in
    JavaScript from numbers, which is how ``it writes nothing`` came to be
    printable at all. ``slots`` and ``drives`` are the limit sentences the
    form shows beside its count inputs, and they carry the number so that the
    page can add its own live total to them without knowing the rule.

    ``holds`` is what the whole form adds up to. A mixed library's totals
    were visible nowhere: a preset of 20 LTO8 and 10 LTO6 made a reader do
    the addition, on the card and in `preset show` alike.
    """
    says = {
        'media': 'Media type (filtered by drive compatibility)',
        'barcode': '',
        'drives': '',
        'slots': '',
        'holds': '',
    }
    read_only = [row['selected'] for row in media['rows'] if row['read_only']]
    if read_only:
        says['media'] = (
            f"{', '.join(read_only)} is read-only in every drive this "
            f"library has: it can be restored from but not backed up to."
            if len(read_only) == 1 else
            f"{', '.join(read_only)} are read-only in every drive this "
            f"library has: they can be restored from but not backed up to.")
    kinds = len([row for row in drives['rows'] if row['selected']])
    says['holds'] = (
        f"{counts['num_drives']} drive(s) of {kinds} kind(s), "
        f"{counts['media_count']} cartridge(s) and "
        f"{counts['empty_slots']} empty slot(s): "
        f"{counts['total_slots']} slots in all")
    if filled.get('barcode_prefix') and filled.get('media_suffix'):
        says['barcode'] = (f"Example: {filled['barcode_prefix']}001"
                           f"{filled['media_suffix']}")
    if model and limits.get('max_drives'):
        says['drives'] = (f"{model} takes at most {limits['max_drives']} "
                          f"drive(s), limited by {limits['bound_by']}")
        if limits['bound_by'] == 'SCSI targets':
            says['drives'] += (f" - {limits['free_targets']} are free, one "
                               f"for the library and one per drive")
    if model and limits.get('max_slots'):
        says['slots'] = f"{model} holds at most {limits['max_slots']} slots"
    return says
