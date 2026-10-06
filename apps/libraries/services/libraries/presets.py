"""Named library configurations: the file, and the operations on it.

A *profile* is a catalogue — what a vendor makes. A *preset* is a
configuration an operator composed from one profile's supported options, under
a name. See docs/sphinx/guides/architecture.rst, "A profile and a preset are
different things".

WHY THE WORK IS SPLIT WITH config/presets.py
--------------------------------------------
config/presets.py turns text into data and back, with no I/O, the way
device_conf.py and library_contents.py do. This module owns everything that
touches the host or needs a rule applied: where the file is, taking the lock,
writing it atomically, and checking a preset against its profile.

The split is forced as well as tidy. The check belongs to validation.py,
which lives here, and config/ may not import libraries/ - that closes the
dependency cycle test_service_layers.py fails on. So storage parses and
renders; the operation validates.

WHAT A PRESET IS, AS DATA
-------------------------
A partial specification: the same dict shape spec.apply_defaults already
completes, carrying only what the operator chose. That is the whole reason
this is cheap — resolve() hands back a dict, the caller merges its own
arguments over it, and everything downstream is unchanged.

INCOMPLETE IS A LEGAL STATE
---------------------------
A preset is built up a piece at a time and the profile may be the last piece,
so `set` accepts `--drives 2` with no profile and says the preset is
incomplete rather than refusing it. Once a profile is there, every `set`
checks the whole preset against it, so a drive the library model cannot take
is caught while authoring rather than at creation.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - writes go through core.locking: lock, write temp, fsync, rename (rule 6)
    - paths come from core.paths (rule 5)
"""
import logging
import re
from typing import Any, Dict, List, Optional

from ..config import presets as presets_format
from ..core import ServiceResult, failure_result, locking, shell, success_result
from ..core.paths import presets_example_path, presets_path
from ..profiles import list_profiles
from . import validation

logger = logging.getLogger(__name__)

#: Name of the lock guarding the preset file. Its own lock, not the MHVTL
#: configuration lock: this file is the console's and a preset write has no
#: reason to wait behind a device.conf write, or to make one wait.
LOCK_NAME = '.presets.lock'


def _path(base=None):
    return presets_path(base)


def _lock(base=None):
    return locking.FileLock(_path(base).parent / LOCK_NAME)


def _example_file(base=None):
    """The shipped example, or None when it is not installed.

    A source checkout has no /etc/mhvtl-gui, and a refusal pointing at a file
    that is not there sends somebody looking for it - worse than saying
    nothing. So existence is checked rather than assumed.
    """
    path = presets_example_path(base)
    try:
        return path if path.is_file() else None
    except OSError:                  # an unreadable directory is "no example"
        return None


def _example(base=None) -> List[str]:
    """The example named as a suggested fix, when there is one to name."""
    path = _example_file(base)
    return [f'every key, with an example of each: {path}'] if path else []


def _unreadable(problem, base=None) -> ServiceResult:
    """The one failure for "the file is there and will not load".

    All four operations here begin by reading the file and all four used to
    spell this out again. What a reader needs is which file - "invalid TOML"
    with no path has sent people to device.conf - and, because a file that
    will not parse has almost always been hand-edited, what a correct one
    looks like.
    """
    return failure_result(str(problem),
                          [f'file: {_path(base)}'] + _example(base))


def load(*, base=None) -> Dict[str, Dict[str, Any]]:
    """Every preset in the file, by name, as a partial specification.

    A missing file is no presets rather than an error: a host that has never
    saved one is not misconfigured. An unreadable or invalid file *is* an
    error, raised as PresetError, because silently reporting "no presets" when
    a file exists would send someone hunting in the wrong place.
    """
    path = _path(base)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as problem:
        raise presets_format.PresetError(
            f'cannot read {path}: {problem}') from problem
    return presets_format.parse(text, profile_names=list_profiles())


def names(*, base=None) -> ServiceResult:
    """The preset names, and whether each one is complete enough to use."""
    try:
        presets = load(base=base)
    except presets_format.PresetError as problem:
        return _unreadable(problem, base)

    rows = []
    for name in sorted(presets):
        spec = presets[name]
        checked = validation.check_partial(spec)
        rows.append({
            'name': name,
            'profile': spec.get('profile'),
            'complete': bool(spec.get('profile')) and checked.is_valid,
            'valid': checked.is_valid,
            'errors': checked.errors,
            'spec': spec,
            # What it fixes, in the words an operator types. The vendor page
            # built this out of the raw specification and a mixed preset has
            # no num_drives or media_type at all - its lists imply them - so
            # the card read "vendor default drive(s)" for a preset holding
            # four drives and thirty cartridges.
            'summary': presets_format.as_words(presets_format.savable(spec)),
            # And what those kinds add up to, for a preset that lists more
            # than one: lib-ten is 30 cartridges and the breakdown made a
            # reader add 20 and 10. `preset show` prints the same sentence.
            'holds': _holds(spec),
            # The same configuration in one line, for a caller that has one:
            # `preset list`'s column and the folded card on the vendor page.
            'brief': _brief(spec),
            # What to type to use it, and to rebuild it on another host.
            # Here rather than in each caller: `preset show` prints these and
            # so does the web console, and a second place that knew how a
            # preset is spelled on the command line is a second place to get
            # it wrong. for_profile() filters these rows, so the vendor page
            # gets them without asking for anything.
            'commands': presets_format.as_commands(name, spec),
        })
    example = _example_file(base)
    return success_result(f'{len(rows)} preset(s) defined',
                          {'presets': rows, 'path': str(_path(base)),
                           'example': str(example) if example else None})


def for_profile(profile_key: str, *, base=None) -> ServiceResult:
    """The presets a vendor's page can offer: the ones that name it.

    A preset names its profile, so the IBM form offers IBM presets and
    nothing else. Offering an STK preset there would be `--preset IBM`'s
    mistake with a mouse - a configuration presented as a choice in a
    catalogue it was not made from.

    Only the usable ones. A half-built preset is legal and is listed by
    `preset list`, but a form that offered one would be offering to create a
    library from a configuration that cannot create a library.
    """
    listed = names(base=base)
    if not listed.success:
        return listed
    wanted = (profile_key or '').upper()
    rows = [row for row in listed.data['presets']
            if (row['profile'] or '').upper() == wanted and row['complete']]
    return success_result(
        f'{len(rows)} preset(s) for {wanted}',
        {'presets': rows, 'profile': wanted,
         'path': listed.data['path'], 'example': listed.data['example']})


def resolve(name: str, *, base=None) -> ServiceResult:
    """One preset as a partial specification, ready for spec.apply_defaults.

    The failure names the presets that do exist, because a mistyped name is
    the moment somebody most wants the list.
    """
    try:
        presets = load(base=base)
    except presets_format.PresetError as problem:
        return _unreadable(problem, base)

    if name not in presets:
        known = ', '.join(sorted(presets)) or 'none are defined'
        return failure_result(
            f"no preset called '{name}'",
            [f'defined: {known}',
             f'file: {_path(base)}',
             'profiles are a different thing: '
             f"{', '.join(sorted(list_profiles()))}"] + _example(base))

    spec = presets[name]

    # A preset carrying `drive` or `media` lists used to be refused here,
    # between 3 and 4 October 2026: the file format took them and creation did
    # not, so one would have been read, accepted and silently ignored, which
    # is worse than a refusal. Creation honours them now - spec.apply_defaults
    # turns either shape into drive_slots and media_runs, and device_conf
    # writes one block per slot - so there is nothing left to refuse. See
    # plan-cli-catalogue, "Finishing it: one canonical form".

    checked = validation.check_partial(spec)
    if not checked.is_valid:
        return failure_result(
            f"preset '{name}' is not usable: " + '; '.join(checked.errors),
            checked.errors + checked.suggested_fixes)
    if not spec.get('profile'):
        return failure_result(
            f"preset '{name}' is incomplete: it names no profile",
            ['a preset needs a profile to take its remaining values from',
             f'mhvtl preset set {name} --profile IBM'])
    return success_result(f"preset '{name}'", {'name': name, 'spec': dict(spec)})


#: What ``apply_defaults`` calls a thing, where it differs from the preset
#: format. It fills ``product`` and ``drive_product`` - the strings that reach
#: SCSI inquiry - and leaves ``library_model`` and ``drive_model`` alone,
#: because those are the operator's words for the same choice. Without this
#: map, describe() would report that a preset leaves the model open and then
#: fail to say what the model would be.
_FILLED_AS = {'library_model': 'product', 'drive_model': 'drive_product'}


def _settled_by_lists(spec: Dict[str, Any]) -> set:
    """The keys a ``drive`` or ``media`` list has already decided.

    ``[[lib-ten.drive]]`` tables say both which models and how many, so a
    preset carrying them has fixed the drive count and the drive model - the
    list *is* the answer. describe() must not then report those as left to the
    profile: `preset show` said "4 drives, 30 tapes" under "left to IBM" for a
    preset whose lists asked for 4 drives and 30 tapes, which read as a
    disagreement between a preset and its profile where there was none.

    The pairing is config/presets.IMPLIED_BY_LIST, the same table savable()
    drops them with, so the file, the save and this answer cannot drift.
    """
    return {key
            for list_key, implied in presets_format.IMPLIED_BY_LIST.items()
            if spec.get(list_key)
            for key in implied}


def _counts_said(spec: Dict[str, Any]) -> str:
    """``'4 drive(s), 30 cartridge(s)'`` - what a specification asks for.

    The numbers are ``spec.asked_drive_count`` and ``asked_media_count``, the
    ones validation uses to ask whether the drives fit the model and the
    cartridges fit the slots - so this cannot say a total that creation
    disagrees with. Either half is left out when the preset does not fix it:
    a preset that names only a model asks for no particular number of
    anything.
    """
    from . import spec as spec_rules

    drives = spec_rules.asked_drive_count(spec, 0)
    tapes = spec_rules.asked_media_count(spec, 0)
    return ', '.join(([f'{drives} drive(s)'] if drives else [])
                     + ([f'{tapes} cartridge(s)'] if tapes else []))


def _holds(spec: Dict[str, Any]) -> str:
    """``'4 drive(s), 30 cartridge(s)'`` - what a preset's lists add up to.

    Only for a preset that holds one: ``as_words`` spells the breakdown -
    ``20 x LTO8 + 10 x LTO6`` - and nothing said thirty, so a reader had to
    add it up. A single-kind preset says ``20 tapes`` there already and a
    second sentence saying twenty would be noise.
    """
    if not (spec.get('drive') or spec.get('media')):
        return ''
    return _counts_said(spec)


def _brief(spec: Dict[str, Any]) -> str:
    """``'IBM, 03584L32, 4 drive(s), 30 cartridge(s)'`` - one line: what this
    preset builds.

    The headline, for a caller with one line to give it: the vendor, the
    model, and what it adds up to - never the breakdown, which is what the
    line opens onto. ``preset list`` prints it in its own column and the
    vendor page's card shows it as the folded summary of each saved
    configuration, where four unfolded cards filled 1.2 screens of a phone.

    A preset that fixes none of those three is spelled by ``as_words``
    instead, which is the fallback and not a second rule: a preset holding
    nothing but ``profile = "IBM"`` is a whole configuration as a concept,
    and "IBM" is the honest one-line version of it.
    """
    said = [part for part in (spec.get('profile'),
                              spec.get('library_model'),
                              _counts_said(spec)) if part]
    return ', '.join(said) or presets_format.as_words(
        presets_format.savable(spec))


def describe(name: str, *, base=None) -> ServiceResult:
    """One preset as a concept: what it fixes, and what it leaves to its
    profile.

    A preset is a profile you built yourself, so showing one has to read like
    showing a profile - not like printing the file it is stored in. A preset
    holding nothing but ``profile = "IBM"`` is one line as a file and a whole
    configuration as a concept, and the second is the useful answer.

    The second half comes from ``spec.apply_defaults``, the same function
    creation fills a specification with, so this cannot disagree with what a
    library would really get.

    ``valid`` and ``complete`` are kept apart on purpose. A half-built preset
    is perfectly valid and simply not finished; one word for both makes it
    sound broken, and "it names no profile yet" is the whole of what is wrong
    with it.
    """
    try:
        presets = load(base=base)
    except presets_format.PresetError as problem:
        return _unreadable(problem, base)

    if name not in presets:
        known = ', '.join(sorted(presets)) or 'none are defined'
        return failure_result(f"no preset called '{name}'",
                              [f'defined: {known}'] + _example(base))

    spec = presets[name]
    checked = validation.check_partial(spec)
    from_profile: Dict[str, Any] = {}

    if spec.get('profile'):
        from . import spec as spec_rules

        try:
            # library_id is required and irrelevant here: nothing in this
            # answer depends on it but the serial and the barcode prefix,
            # neither of which a preset can hold.
            filled = spec_rules.apply_defaults({**spec, 'library_id': 0})
        except spec_rules.UnknownProfile:
            filled = {}
        settled = _settled_by_lists(spec)
        for key in presets_format.KEY_TO_SPEC.values():
            if key in spec or key in settled:
                continue
            source = _FILLED_AS.get(key, key)
            if source in filled:
                from_profile[key] = filled[source]

    return success_result(
        f"preset '{name}'",
        {'name': name, 'spec': dict(spec), 'profile': spec.get('profile'),
         'fixed': presets_format.savable(spec), 'from_profile': from_profile,
         # What the kinds add up to, for a preset that lists more than one,
         # and the whole of it in one line.
         'holds': _holds(spec),
         'brief': _brief(spec),
         'commands': presets_format.as_commands(name, spec),
         'valid': checked.is_valid,
         'complete': bool(spec.get('profile')) and checked.is_valid,
         'errors': list(checked.errors),
         'warnings': list(checked.warnings),
         'fixes': list(checked.suggested_fixes)})


#: What a preset may be called: TOML's own bare-key characters.
#:
#: The name is written into the file as ``[name]``, so a name outside this
#: set writes a file that will not parse - and takes every other preset with
#: it, since one bad table stops the whole file loading. A dot is the one
#: that looks harmless: ``[lab.small]`` is a *nested table* in TOML, so the
#: preset would come back as a key called 'small' inside one called 'lab'
#: and the file would refuse to load with "unknown key 'small'".
USABLE_NAME = re.compile(r'^[A-Za-z0-9_-]+$')


def _unusable(name: str) -> Optional[ServiceResult]:
    """The refusal when a name cannot be a preset's, or None when it can.

    Both rules in one place because both writers need them: ``save`` creates
    the name and ``rename`` moves one, and a check in one of them only is a
    file that can be corrupted by the other.
    """
    if name.upper() in {key.upper() for key in list_profiles()}:
        return failure_result(
            f"'{name}' is a vendor profile, not a configuration",
            ['a preset is built from a profile and cannot share its name',
             f'try a name of your own: {name.lower()}-small'])
    if not USABLE_NAME.match(name or ''):
        return failure_result(
            f"'{name}' cannot be a preset name",
            ['letters, digits, - and _ only: the name is written into the '
             'file as a TOML table header',
             'a name with a dot or a bracket would stop the whole file '
             'loading, not just this preset'])
    return None


def save(name: str, values: Dict[str, Any], *, replace_lists=(),
         base=None) -> ServiceResult:
    """Merge values into a preset, creating it if it is new.

    Scalars replace. Lists - drive and media - append, which is what building
    one up a piece at a time means; naming a list in replace_lists replaces it
    instead, so `--drive` can overwrite what `--add-drive` accumulated.

    The whole preset is checked after merging, not just what arrived: adding a
    drive the library model cannot take is wrong even though the drive itself
    is a valid model.
    """
    refused = _unusable(name)
    if refused is not None:
        return refused

    with _lock(base):
        try:
            presets = load(base=base)
        except presets_format.PresetError as problem:
            return _unreadable(problem, base)

        merged = dict(presets.get(name, {}))
        for key, value in values.items():
            if key in presets_format.LIST_KEYS and key not in replace_lists:
                merged[key] = list(merged.get(key, [])) + list(value)
            else:
                merged[key] = value

        checked = validation.check_partial(merged)
        if not checked.is_valid:
            return failure_result(
                f"preset '{name}' would not be valid: "
                + '; '.join(checked.errors),
                checked.errors + checked.suggested_fixes)

        presets[name] = merged
        written = _write(presets, base=base)

    if not written.success:
        return written
    return success_result(
        f"preset '{name}' saved",
        {'name': name, 'spec': merged,
         'complete': bool(merged.get('profile')),
         'warnings': checked.warnings, 'path': str(_path(base))})


def rename(old: str, new: str, *, base=None) -> ServiceResult:
    """Give a preset another name, keeping everything it holds.

    The only thing that moves is the key. Nothing is revalidated, because
    nothing about the configuration changed - a half-built preset renames as
    happily as a finished one, and a preset that was valid under one name
    cannot become invalid under another.

    An existing name is refused rather than written over: renaming onto a
    preset would discard what that one holds, silently, and the operator who
    typed it would have no way to know. The same two guards `save` applies to
    a new name apply here - a vendor's name, and a name the file cannot hold.

    Until 5 October 2026 nothing could do this: not the command line, not the
    console, not a service. Renaming meant editing /etc/mhvtl-gui/presets.toml
    by hand, which is root-owned, so the console's own user could not.
    """
    refused = _unusable(new)
    if refused is not None:
        return refused

    with _lock(base):
        try:
            presets = load(base=base)
        except presets_format.PresetError as problem:
            return _unreadable(problem, base)

        if old not in presets:
            known = ', '.join(sorted(presets)) or 'none are defined'
            return failure_result(f"no preset called '{old}'",
                                  [f'defined: {known}'] + _example(base))
        if new == old:
            return failure_result(f"preset '{old}' is already called that",
                                  ['nothing was changed'])
        if new in presets:
            return failure_result(
                f"a preset called '{new}' already exists",
                ['renaming onto it would throw away what it holds',
                 f'mhvtl preset show {new}',
                 f'mhvtl preset delete {new}'])

        presets[new] = presets.pop(old)
        written = _write(presets, base=base)

    if not written.success:
        return written
    return success_result(f"preset '{old}' is now '{new}'",
                          {'name': new, 'was': old, 'spec': presets[new],
                           'path': str(_path(base))})


def forget(name: str, keys: List[str], *, base=None) -> ServiceResult:
    """Remove keys from a preset, or the whole preset when keys is empty."""
    with _lock(base):
        try:
            presets = load(base=base)
        except presets_format.PresetError as problem:
            return _unreadable(problem, base)

        if name not in presets:
            known = ', '.join(sorted(presets)) or 'none are defined'
            return failure_result(f"no preset called '{name}'",
                                  [f'defined: {known}'])

        if not keys:
            del presets[name]
            message = f"preset '{name}' deleted"
        else:
            unknown = [k for k in keys if k not in presets[name]]
            if unknown:
                return failure_result(
                    f"preset '{name}' has no {', '.join(unknown)}",
                    [f"it has: {', '.join(sorted(presets[name]))}"])
            for key in keys:
                del presets[name][key]
            message = f"preset '{name}': removed {', '.join(keys)}"

        written = _write(presets, base=base)
    return written if not written.success else success_result(message)


def _write(presets: Dict[str, Dict[str, Any]], *, base=None) -> ServiceResult:
    """Render and replace the file in one step, under the caller's lock.

    PermissionError rather than a crash when the directory is root-owned and
    the caller is not: the message says which, because "Permission denied" on
    its own has sent people to the wrong file more than once.
    """
    path = _path(base)
    text = presets_format.render(presets)
    try:
        locking.atomic_write_text(path, text)
    except PermissionError:
        # The fallback ConfigService.write already uses for /etc/mhvtl: the
        # web runs as mhvtl-gui, the file is root-owned, and tee is in the
        # sudoers rules. Without this the wizard could read presets and never
        # save one, and it is the caller's lock that makes a tee acceptable
        # here - the lock is held for the whole of _write.
        written = shell.sudo_tee(path, text)
        if not written.ok:
            return failure_result(
                f'cannot write {path}',
                [f'{path.parent} is not writable by this user',
                 written.stderr.strip() or 'and sudo tee did not write it '
                 'either; run the command with sudo, as the other mutating '
                 'commands need'])
    except OSError as problem:
        return failure_result(f'cannot write {path}: {problem}')
    logger.info('wrote %d preset(s) to %s', len(presets), path)
    return success_result(f'{len(presets)} preset(s) written', {'path': str(path)})
