"""The console's own preferences: /etc/mhvtl-gui/settings.toml.

Today it holds one thing - how big a new cartridge is - and it is built to hold
more, because the first setting is the expensive one and the fifth should be a
line.

WHY THERE IS A FILE AT ALL
--------------------------
A cartridge used to be created at its density's native capacity: an LTO-8 is
12 TB, which is true about the hardware and wrong about this software. At the
55 MB/s this host writes, filling one takes 63 hours, so end-of-tape handling
and multi-volume spanning - the things a virtual library exists to exercise -
could not be tested at the default. It costs no disk, because the media files
are sparse; it costs time, which is worse.

So the default is 1,000 MB, and anyone who wants a realistic cartridge says so
here. The native capacities stay in profiles/personalities.NATIVE_CAPACITY_GB,
transcribed from MHVTL and checked against it: this file holds *choices*, that
table holds *facts*, and an operator editing this one never changes what an
LTO-8 is.

THE CHAIN
---------
Four levels, each narrower than the last, and there is no fifth:

    1. the code                1,000 MB, for every density
    2. tape.size.default       this file, for every density
    3. tape.size.<density>     this file, for one
    4. --size-mb / the form    this cartridge, this once

`tapes.service.size_for` walks 1 to 3; the creation verbs apply 4. A number
anywhere else is a bug - two of them disagreeing is what
docs/sphinx/guides/plan-tape-size.rst exists to stop happening again.

WHY IT IS IN config/
--------------------
Because `tapes` needs to read it, and `tapes` may import `config`, `core` and
`profiles` and nothing else (test_service_layers.ALLOWED). It sits beside
presets.py, which does the same job for the other file the operator owns -
except that presets.py is pure format, with libraries/presets.py doing its I/O.
This one does both, which config already does in device_conf.py and service.py,
because the alternative would put the reading in a layer `tapes` cannot reach.

Rules for this layer:
    - returns plain values; a caller cannot tell a miss from a failure
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
"""
import logging
import tomllib
from typing import Any, Dict, Optional

from ..core import atomic_write_text, sudo_tee
from ..core.paths import settings_path
from ..profiles import personalities

logger = logging.getLogger(__name__)

#: What a new cartridge is, for every density, before anybody says otherwise.
#:
#: 1,000 rather than 1,024: UNKNOWN_SIZE_MB in tapes/service.py is already
#: 1,000 - it is what a density MHVTL gives no native capacity (9840, 9940)
#: has always received - so promoting that number leaves the console with one
#: idea of a gigabyte instead of two.
DEFAULT_TAPE_SIZE_MB = 1000

#: The section and key for the fallback that applies to every density.
#:
#: `default`, not `default_mb`: every key under [tape.size] is a size in MB -
#: `LTO8 = 1000` carries no suffix either - and the header says so once. A
#: suffix on one key and not the others is the kind of small disagreement this
#: file exists to stop.
TAPE_SIZE = 'tape.size'
TAPE_SIZE_DEFAULT = f'{TAPE_SIZE}.default'


def read(base=None) -> Dict[str, Any]:
    """The whole file, as nested dictionaries. ``{}`` when there is none.

    A missing file is the normal state, not an error: it means nothing has
    been overridden. A corrupt one is logged and treated the same way, because
    a settings page that cannot render is worse than one showing the defaults.
    """
    path = settings_path(base)
    try:
        with open(path, 'rb') as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as problem:
        # No traceback: a TOMLDecodeError already says which line and column,
        # which is the whole of what a reader needs, and this is a warning
        # about somebody's hand edit rather than a fault in this code.
        logger.warning('%s could not be read (%s); using the defaults',
                       path, problem)
        return {}


def get(key: str, base=None, data: Dict[str, Any] = None) -> Optional[Any]:
    """One dotted key - ``tape.size.LTO8`` - or None when it is not set.

    The dots are the path through the file, so what a reader types is what
    they will find in it. That is the rule presets follows for its keys and
    the reason `mhvtl settings set tape.size.LTO8` needs no translation table.
    """
    node: Any = read(base) if data is None else data
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return None if isinstance(node, dict) else node


def tape_size_mb(density: str, base=None,
                 data: Dict[str, Any] = None) -> int:
    """How big a new cartridge of this density should be - levels 1 to 3.

    The density's own entry, then the file's default, then the code's. Never
    the native capacity: that is a different question, answered by
    tapes.service.native_mb, and the whole point of this file is that the two
    are no longer the same number.
    """
    data = read(base) if data is None else data
    named = get(f'{TAPE_SIZE}.{(density or "").upper()}', data=data)
    if isinstance(named, int) and named > 0:
        return named
    fallback = get(TAPE_SIZE_DEFAULT, data=data)
    if isinstance(fallback, int) and fallback > 0:
        return fallback
    return DEFAULT_TAPE_SIZE_MB


def source_of(density: str, base=None,
              data: Dict[str, Any] = None) -> str:
    """Which level of the chain decided it, for `settings list` and the page.

    "Why is this cartridge 1 GB" is the question both front ends exist to
    answer, and a number with no provenance does not answer it.

    A PINNED DEFAULT IS NOT A CHOICE SOMEBODY MADE
    ----------------------------------------------
    `render` writes `default` into the file whether or not anybody set it, so
    that changing the shipped default in a later release cannot silently
    resize an existing host's cartridges. That is deliberate and worth
    keeping, but it used to make this say "the file default" for every
    density on a host where one density had been changed - and the `list`
    summary then reported all thirty-three as changed.

    So a file default equal to the shipped one is reported as the shipped
    default. It is where the number came from either way; what it is not is
    an answer to "what did I change", and that is what the column is read
    for.
    """
    data = read(base) if data is None else data
    if isinstance(get(f'{TAPE_SIZE}.{(density or "").upper()}', data=data), int):
        return 'set for this density'
    fallback = get(TAPE_SIZE_DEFAULT, data=data)
    if isinstance(fallback, int) and fallback != DEFAULT_TAPE_SIZE_MB:
        return 'the file default'
    return 'the shipped default'


# -- reading what somebody typed -------------------------------------------

#: Decimal, because tape capacity is quoted decimal and this table is
#: transcribed from one: native_mb is gigabytes x 1000, so an LTO-8 is
#: 12,000,000 MB. MiB and GiB are deliberately not accepted - 12 TB and 12 TiB
#: differ by ten per cent, and two unit systems in one field is how a number
#: that is supposed to match real hardware stops matching it.
_UNITS = {'MB': 1, 'GB': 1000, 'TB': 1000 * 1000}


def parse_size_mb(text) -> int:
    """``"12TB"``, ``"2000 GB"``, ``1000`` -> a size in MB.

    Raises ValueError with a sentence a person can act on, because this reads
    what somebody typed into a form or a command line.
    """
    if isinstance(text, int):
        value, unit = text, 'MB'
    else:
        cleaned = str(text).strip().replace(',', '').replace(' ', '').upper()
        if not cleaned:
            raise ValueError('no size given')
        if cleaned[-3:] in ('MIB', 'GIB', 'TIB'):
            raise ValueError(
                f'{text}: sizes here are decimal - MB, GB or TB. A tape is '
                f'quoted decimal, so an LTO-8 holds 12 TB and not 12 TiB')
        unit = next((u for u in ('TB', 'GB', 'MB') if cleaned.endswith(u)), 'MB')
        number = cleaned[:-len(unit)] if cleaned.endswith(unit) else cleaned
        try:
            value = float(number)
        except ValueError:
            raise ValueError(f'{text}: not a size. Try 1000, 2000GB or 12TB')

    size = int(value * _UNITS[unit])
    if size <= 0:
        raise ValueError(f'{text}: a cartridge cannot be {size} MB')
    return size


def as_size(mb: int) -> str:
    """A size in MB, for a person: 12,000,000 -> "12 TB (12,000,000 MB)".

    Both, because the two readers want different ones: an operator reads the
    table in TB, and anybody about to type a number into the field beside it
    needs the MB, which is the only unit the field takes.
    """
    rounded = _rounded(mb)
    return f'{rounded} ({mb:,} MB)' if rounded else f'{mb:,} MB'


def as_short_size(mb: int) -> str:
    """The same size with no second opinion: 12,000,000 -> "12 TB".

    For a drop-down option. "LTO8 (12 TB (12,000,000 MB))" is two sets of
    brackets and one number too many, and the option is a label rather than
    something anybody copies a figure out of. Where there is room for both -
    a settings table, a field's help line - use as_size.
    """
    return _rounded(mb) or f'{mb:,} MB'


def _rounded(mb: int) -> str:
    """"12 TB", "2.5 TB", "20 GB" - or '' when MB is the honest unit.

    One decimal place, because an LTO-6 holds 2.5 TB and saying 2500 GB is
    correct and not how anybody quotes it.

    Only an exact division is rounded. 1,048 MB is not "1 GB": an operator
    comparing it with the 1,000 MB they typed would be given no way to see
    that the two are different, which is the one thing the label is for.
    """
    for unit, factor in (('TB', 1000 * 1000), ('GB', 1000)):
        if mb >= factor and (mb * 10) % factor == 0:
            value = mb / factor
            shown = f'{value:.0f}' if value.is_integer() else f'{value:.1f}'
            return f'{shown} {unit}'
    return ''


# -- writing ---------------------------------------------------------------

def put(key: str, value: Any, base=None,
        data: Dict[str, Any] = None) -> Dict[str, Any]:
    """Set one dotted key and return the data. Writes nothing.

    ``data`` lets a caller changing several keys do so against one reading
    rather than re-reading the file per key - which would also lose every
    change but the last.
    """
    data = read(base) if data is None else data
    node = data
    parts = key.split('.')
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    return data


def drop(key: str, base=None, data: Dict[str, Any] = None) -> Dict[str, Any]:
    """Remove one dotted key, and any section it leaves empty.

    ``data`` as for put(): one reading for a caller changing several keys.
    """
    data = read(base) if data is None else data
    parts = key.split('.')
    chain = [data]
    node: Any = data
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return data
        chain.append(node)
    node.pop(parts[-1], None)

    # An empty [tape.size] left behind reads as "somebody configured this and
    # it came to nothing", which is not what a reset means.
    for parent, part in zip(reversed(chain[:-1]), reversed(parts[:-1])):
        if isinstance(parent.get(part), dict) and not parent[part]:
            parent.pop(part)
    return data


def write(data: Dict[str, Any], base=None) -> bool:
    """Render and save, atomically. True when it landed.

    The fallback is the one ConfigService and presets already use: the web
    runs as mhvtl-gui, the file is root-owned, and tee is in the packaged
    sudoers rules. A failure to write is reported by the caller, never raised
    here - a page must not break because a directory is read-only.
    """
    path = settings_path(base)
    text = render(data)
    try:
        atomic_write_text(path, text)
        return True
    except PermissionError:
        return bool(sudo_tee(path, text).ok)
    except OSError:
        logger.warning('%s could not be written', path, exc_info=True)
        return False


def render(data: Dict[str, Any]) -> str:
    """The file as text, with the native capacities written in beside it.

    The comments are the reference an operator needs to put a density back the
    way it was without reading the documentation, and they are regenerated on
    every write - so editing a value can never orphan the note next to it.
    """
    sizes = (data.get('tape') or {}).get('size') or {}

    # `default` is written even when nobody set it, which has two effects and
    # both are wanted. It shows the knob to whoever opens the file, and it
    # pins the value: once a host has a settings file, changing the shipped
    # default in a later release cannot silently move that host's cartridges.
    # The cost is that source_of() then says "the file default" rather than
    # "the shipped default", which is accurate - the file really is where the
    # number came from.
    lines = [_HEADER, '[tape.size]',
             '# Any density not named below gets this. In MB, like the rest.',
             f'default = {sizes.get("default", DEFAULT_TAPE_SIZE_MB)}',
             '']

    named = {key: value for key, value in sizes.items() if key != 'default'}
    if named:
        width = max(len(key) for key in named)
        for density in sorted(named):
            capacity = personalities.NATIVE_CAPACITY_GB.get(density.upper())
            note = (f'  # native {capacity * 1000:,} MB'
                    if capacity else '  # no native capacity')
            lines.append(f'{density.ljust(width)} = {named[density]}{note}')
        lines.append('')

    for section, body in sorted(data.items()):
        if section == 'tape':
            continue
        lines.append(f'[{section}]')
        for key, value in sorted((body or {}).items()):
            lines.append(f'{key} = {_value(value)}')
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


#: The comment every written file opens with. Someone who has opened this file
#: and read no documentation should still be able to change a value.
_HEADER = '''\
# The console's own settings. Written by `mhvtl settings set` and by the
# Settings page; safe to edit by hand.
#
#   mhvtl settings list                  every setting, and where each came from
#   mhvtl settings get tape.size.LTO8    one of them
#   mhvtl settings set tape.size.LTO8 12TB
#   mhvtl settings reset tape.size.LTO8  back to the shipped default
#
# Sizes are in MB and decimal, as tape capacities are quoted: an LTO-8 holds
# 12,000,000 MB. MB, GB and TB are accepted when setting one; MB is stored.
#
# A density not named here gets `default`. Delete this file and every
# cartridge goes back to the shipped default.
'''
