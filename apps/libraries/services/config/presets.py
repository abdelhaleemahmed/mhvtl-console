"""Named library configurations: TOML text in, specifications out.

A **profile** is a catalogue — what a vendor makes, which drives each library
model accepts, which media each drive writes, and a default for each. It
ships in profiles/data.py and nothing edits it.

A **preset** is a configuration: one set of choices made from a profile's
supported options, under a name. Every preset is local. There is no such
thing as a shipped preset, and a preset may not take a profile's name,
because `--profile IBM` and `--preset IBM` would then differ in a way nobody
could see.

TEXT IN, DATA OUT - NO FILE I/O HERE
------------------------------------
Like device_conf.py and library_contents.py beside it: parse() takes text and
render() returns text, so both are testable against a string and reusable
without Django. Reading the file, writing it under the lock and validating
what comes out belong to libraries/presets.py, which owns the operation.

WHY TOML
--------
tomllib is in the standard library from 3.11 and this project requires 3.12,
so parsing costs no dependency - PyYAML would be a new package on every
installed host for a file two levels deep. TOML also allows comments, which a
file people edit by hand needs and JSON forbids. Rendering is written out
here rather than taken from a library because tomllib does not write, and the
subset a preset needs is small enough to spell.

THE KEYS ARE THE COMMAND LINE'S
-------------------------------
`drives`, `tapes`, `drive`, `media`, `model`, `density` - what an operator
types is what the file holds, so a preset can be read as the command it
replaces. One table, KEY_TO_SPEC, turns them into the specification dict's
own names (num_drives, media_count) in one place rather than scattered
through the callers.

Rules for this layer:
    - no file I/O and no subprocess: text in, data out
    - returns plain data, not a ServiceResult; the operation that calls this
      returns the ServiceResult
"""
import shlex
import tomllib
from typing import Any, Dict, List, Tuple

#: What the command line calls a thing -> what the specification dict calls it.
#: Only the scalars; `drive` and `media` are lists and are handled apart.
#:
#: This is the whole of what a preset may hold, and the order is the order a
#: rendered file and `preset show` use. Two keys were taken out of it on
#: 3 October 2026, both because a preset carrying them was a lie:
#:
#: `serial` - a unit serial number is per library, and a preset exists to be
#: used again. Two libraries from one preset would have shared it. It stays an
#: option on `library create`, where it applies to the one library being made,
#: and --save-preset never kept it.
#:
#: `size_mb` - nothing read it. The key the creation path reads is
#: `tape_size_mb` (libraries/workflow.py:243), so a preset asking for 20000
#: would have been parsed, stored, validated and then ignored - the thing the
#: mixed-drive refusal in libraries/presets.py exists to prevent.
#:
#: mhvtl_cli's _SAVABLE is these values; test_presets.py pins the two
#: together, so an option cannot be added to one and forgotten in the other.
KEY_TO_SPEC = {
    'profile': 'profile',
    'model': 'library_model',
    'drive_model': 'drive_model',
    'drive_revision': 'drive_revision',
    'media_type': 'media_type',
    'drives': 'num_drives',
    'tapes': 'media_count',
    'empty_slots': 'empty_slots',
}

#: Keys inside a [[name.drive]] or [[name.media]] table.
LIST_KEYS = {'drive': ('model', 'count'), 'media': ('density', 'count')}

#: What a list settles, so that nothing stores it twice or reports it as
#: still open. `[[x.drive]]` tables say both which models and how many, so a
#: preset carrying them has no business also carrying `drives` or a single
#: `drive_model` - the file refuses the first pair outright, savable() drops
#: them, and `preset show` must not list them as left to the profile when the
#: list has already decided them.
IMPLIED_BY_LIST = {'drive': ('num_drives', 'drive_model'),
                   'media': ('media_count', 'media_type')}


class PresetError(ValueError):
    """The file cannot be read, or a preset in it makes no sense.

    Deliberately not a ServiceResult: this is raised by a parser and caught by
    the operation, which turns it into one.
    """


def parse(text: str, *, profile_names=()) -> Dict[str, Dict[str, Any]]:
    """Every preset in the file, by name, as a partial specification.

    profile_names is the list of vendor profiles, so a preset that takes one
    of their names is refused here rather than confusing a caller later.

    An unknown key is an error, never ignored: a preset whose `drive-model`
    silently did nothing would be worse than one that would not load.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as problem:
        raise PresetError(f'presets are not valid TOML: {problem}') from problem

    reserved = {name.upper() for name in profile_names}
    presets: Dict[str, Dict[str, Any]] = {}
    for name, body in raw.items():
        if name.upper() in reserved:
            raise PresetError(
                f"'{name}' is a vendor profile, not a configuration; "
                f'choose another name for the preset')
        if not isinstance(body, dict):
            raise PresetError(f"preset '{name}' must be a table, "
                              f'not {type(body).__name__}')
        presets[name] = _one(name, body)
    return presets


def _one(name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    spec: Dict[str, Any] = {}
    for key, value in body.items():
        if key in LIST_KEYS:
            spec[key] = _list(name, key, value)
            continue
        if key not in KEY_TO_SPEC:
            raise PresetError(
                f"preset '{name}': unknown key '{key}'. "
                f"known keys: {', '.join(sorted(KEY_TO_SPEC) + sorted(LIST_KEYS))}")
        spec[KEY_TO_SPEC[key]] = value

    # A count and a list are two answers to one question, and nothing can
    # choose between them: `drives = 4` beside two [[name.drive]] tables could
    # mean four drives or two kinds of drive, and a library created from the
    # wrong reading is the wrong size. The list is the richer form, so the
    # refusal says to drop the count.
    for count_key, list_key in (('drives', 'drive'), ('tapes', 'media')):
        if count_key in body and list_key in body:
            raise PresetError(
                f"preset '{name}': '{count_key}' and '[[{name}.{list_key}]]' "
                f"are two answers to one question. The list says how many of "
                f"each, so remove '{count_key}'")
    return spec


def _list(name: str, key: str, value: Any) -> List[Dict[str, Any]]:
    """A [[name.drive]] or [[name.media]] array, in the order it was written.

    Order is slot order and therefore SCSI target order, so it is preserved
    exactly: a backup application that addresses drives by position notices
    when they move.
    """
    what, count_key = LIST_KEYS[key]
    if not isinstance(value, list):
        raise PresetError(f"preset '{name}': '{key}' must be a list of tables "
                          f'- [[{name}.{key}]] - not {type(value).__name__}')
    entries = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise PresetError(f"preset '{name}': {key} {index} must be a table")
        unknown = set(entry) - {what, count_key}
        if unknown:
            raise PresetError(
                f"preset '{name}': {key} {index} has unknown key(s) "
                f"{', '.join(sorted(unknown))}; known keys: {what}, {count_key}")
        if what not in entry:
            raise PresetError(f"preset '{name}': {key} {index} needs a {what}")
        count = entry.get(count_key, 1)
        if not isinstance(count, int) or count < 1:
            raise PresetError(f"preset '{name}': {key} {index} count must be "
                              f'a positive whole number, not {count!r}')
        entries.append({what: entry[what], count_key: count})
    return entries


def savable(spec: Dict[str, Any]) -> Dict[str, Any]:
    """What a preset keeps out of a complete specification.

    The keys this format holds and only those. Not library_id - a preset is
    used again for the next library, and the id is the one thing that must
    differ - and not the aliases apply_defaults derives (product,
    drive_product), which would be the same fact stored twice under two names,
    free to disagree.

    Here rather than in mhvtl_cli, where it started: `--save-preset` and the
    setup wizard's "keep this configuration" are one rule, and the web cannot
    import the CLI. plan-cli-catalogue.rst predicted that for this step, and
    this is it - the rule had to move before the second caller could exist.

    The `drive` and `media` lists are kept as well, so that saving a mixed
    library captures what makes it mixed. They are kept *instead of* the
    counts they imply, because the file refuses both together - a count and a
    list are two answers to one question.
    """
    kept = {key: spec[key] for key in KEY_TO_SPEC.values() if key in spec}
    # A list replaces what it implies: the count, and the single model or
    # density it would otherwise be read beside. Keeping both would store the
    # same fact twice - and `media_type` beside a media list is worse than
    # redundant, because a hand-edit could make the nominal density contradict
    # the run that sets it.
    for list_key, implied in IMPLIED_BY_LIST.items():
        if spec.get(list_key):
            kept[list_key] = [dict(entry) for entry in spec[list_key]]
            for key in implied:
                kept.pop(key, None)
    return kept


#: What each stored key is called as a `preset set` option.
#:
#: A third spelling of the same choice, and the reason this table exists
#: rather than being derived: the file says ``model``, the specification says
#: ``library_model`` and the flag is ``--model``. KEY_TO_SPEC maps the first
#: two; this maps the second to the third.
#:
#: The order is KEY_TO_SPEC's, so a rebuilt command reads in the same order
#: as the file and as `preset show` - profile first, then what it fixes.
#:
#: test_presets hands every command this composes to the real argparse
#: parser, so a flag renamed in commands/preset.py and not here is caught by
#: the test rather than by somebody pasting a command that does not run.
FLAG_FOR = {
    'profile': '--profile',
    'library_model': '--model',
    'drive_model': '--drive-model',
    'drive_revision': '--drive-revision',
    'media_type': '--media-type',
    'num_drives': '--drives',
    'media_count': '--tapes',
    'empty_slots': '--empty-slots',
}

#: The two lists, which repeat their flag once per entry: `--drive X:2`.
FLAG_FOR_LIST = {'drive': ('--drive', 'model'),
                 'media': ('--media', 'density')}


def as_words(values: Dict[str, Any]) -> str:
    """``IBM, model 03584L32, 2 drives, 20 tapes`` - a preset, read aloud.

    The words an operator types, in the file's own order: ``num_drives`` is
    what the specification calls it and ``drives`` is what anybody typing it
    calls it. A list is spelled as the counts it holds - ``2 x ULT3580-TD8 +
    2 x ULT3580-TD6`` - in file order, because that is slot order and a
    drive's position is part of what was asked for.

    Takes either half of what `describe` returns: what a preset fixes, or
    what it leaves to its profile.

    IT WAS IN mhvtl_cli
    -------------------
    `preset show` composed this and the web could not reach it, so the vendor
    page wrote its own summary out of the raw specification - and a mixed
    preset has no ``num_drives`` or ``media_type`` at all, because its lists
    imply them. The card said *"03584L32, vendor default drive(s), vendor
    default x vendor default"* for a preset holding four drives and thirty
    cartridges. One spelling, two callers.
    """
    spoken = {spec: key for key, spec in KEY_TO_SPEC.items()}
    said = []
    for key, value in values.items():
        if key in LIST_KEYS:
            what = LIST_KEYS[key][0]
            said.append(' + '.join(f"{entry['count']} x {entry[what]}"
                                   for entry in value))
            continue
        word = spoken.get(key, key)
        if word in ('drives', 'tapes', 'empty_slots'):
            said.append(f"{value} {word.replace('_', ' ')}")
        elif word == 'profile':
            said.append(str(value))
        else:
            said.append(f"{word.replace('_', ' ')} {value}")
    return ', '.join(said)


def as_commands(name: str, spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """The commands that use this preset, and that rebuild it elsewhere.

    ``[{'label', 'command'}, ...]`` - finished lines, because a caller that
    composed them would be the second place that knows how a preset is
    spelled on the command line. `preset show` prints them and the web
    console prints the same ones; the browser builds nothing.

    WHY TWO
    -------
    *Use it* answers "what do I type to get this library", which is the
    question somebody reading a preset actually has. *Rebuild it* is the one
    that survives leaving this host: ``presets.toml`` does not travel, so a
    preset is only as portable as the command that recreates it.

    NO --id
    -------
    ``library create`` allocates the next free id when none is given, so the
    shorter command is also the more correct one - and the id is a property
    of a host, which this module cannot see and should not pretend to.

    A preset with no profile yet gets only the second line. ``--preset`` on a
    preset that names no profile is refused, and offering a command that
    cannot run is the mistake `profile list` made for a week with
    ``mhvtl library models``.
    """
    commands = []
    if spec.get('profile'):
        commands.append({'label': 'Use it',
                         'command': f'mhvtl library create --preset {name}'})
    commands.append({
        'label': 'Rebuild it',
        'command': ' '.join(['mhvtl preset set', name] + _as_flags(spec))})
    return commands


def _as_flags(spec: Dict[str, Any]) -> List[str]:
    """Every option `preset set` would need to write this preset again.

    A list's flag is repeated once per entry, and the keys that list implies
    are left out - naming both is refused, so composing both would produce a
    command this project's own parser rejects.
    """
    settled = {key for list_key, implied in IMPLIED_BY_LIST.items()
               if spec.get(list_key) for key in implied}
    said = []
    for key, flag in FLAG_FOR.items():
        if key in spec and key not in settled:
            said.append(f'{flag} {shlex.quote(str(spec[key]))}')
    for key, (flag, what) in FLAG_FOR_LIST.items():
        for entry in spec.get(key) or []:
            # MODEL:COUNT is one argument, so the quoting goes round both of
            # them: `--drive 'SDLT 320:2'`, not `--drive 'SDLT 320':2`.
            run = f"{entry[what]}:{entry['count']}"
            said.append(f'{flag} {shlex.quote(run)}')
    return said


def render(presets: Dict[str, Dict[str, Any]]) -> str:
    """The file text for these presets, with the keys an operator types.

    Written rather than taken from a library because tomllib does not write.
    The output is deliberately plain: one table per preset, the scalars first
    in the command line's own order, then any drive and media arrays.
    """
    spec_to_key = {spec: key for key, spec in KEY_TO_SPEC.items()}
    order = list(KEY_TO_SPEC)
    lines = [_HEADER]
    for name in sorted(presets):
        spec = presets[name]
        lines.append(f'[{name}]')
        for key in order:
            value = spec.get(KEY_TO_SPEC[key])
            if value is not None and KEY_TO_SPEC[key] in spec:
                lines.append(f'{key} = {_value(value)}')
        for key, (what, count_key) in LIST_KEYS.items():
            for entry in spec.get(key, []):
                lines.append('')
                lines.append(f'[[{name}.{key}]]')
                lines.append(f'{what} = {_value(entry[what])}')
                lines.append(f'{count_key} = {entry[count_key]}')
        lines.append('')
    return '\n'.join(lines)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


#: The comment at the top of a written file. The point of it is that someone
#: who opens the file and has never read the documentation can still add a
#: preset, which is the whole reason the keys are the command line's.
#:
#: The same text goes into presets.toml and into the shipped
#: presets.toml.example - test_it_carries_the_header_a_written_file_gets holds
#: the two together - so every sentence here has to be true read from either
#: file. The last one was not: "the file beside this one: presets.toml.example"
#: is a direction in presets.toml and a circle in the example, where it sent
#: the reader to the file already open in front of them. It names the file
#: now instead of pointing at a neighbour.
_HEADER = '''\
# Named library configurations for `mhvtl library create --preset NAME`.
#
# Each table is one configuration, built from a vendor profile's supported
# options. The keys are the ones you would type on the command line, so a
# preset reads as the command it replaces.
#
#   mhvtl profile list                the vendor catalogues to build from
#   mhvtl profile show IBM            one of them: models, drives, densities
#   mhvtl preset list                 what is defined here
#   mhvtl preset show NAME            what one fixes, and what it leaves open
#   mhvtl preset set NAME             add to or change one, key by key
#   mhvtl preset rename OLD NEW       give one another name
#
# A preset may not take a profile's name (IBM, STK, ...): a profile is a
# catalogue of what a vendor makes, and a preset is one configuration built
# from it. Both are templates; neither is ever created.
#
# Mixed drives and media are arrays, in slot order - which is also the order
# of the SCSI targets, so the first table is the first drive:
#
#   [[lib-ten.drive]]
#   model = "ULT3580-TDA"
#   count = 2
#
#   [[lib-ten.drive]]
#   model = "ULT3580-TD9"
#   count = 2
#
# A list says both which models and how many, so it replaces `drives` (and
# `media` replaces `tapes`): naming both is refused, because a count and a
# list are two answers to one question and nothing can choose between them.
#
# presets.toml.example, installed in this directory, carries a worked example
# of every key: one library per vendor at the newest tape its drives write.
'''
