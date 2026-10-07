"""Reading and changing the console's own settings.

Every verb the Settings page offers and `mhvtl settings` offers is here, so
the two cannot answer differently. The page renders what this returns; the
command prints it.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell - by way of config/settings
"""
import logging
from typing import Dict, List, Optional

from ..config import settings as file
from ..core import ServiceResult, failure_result, success_result
from ..profiles import personalities

logger = logging.getLogger(__name__)

#: Every key this version knows, so `list` can show one that has never been
#: set and `set` can refuse one that will never be read. A key nothing reads
#: is worse than a missing one: it is accepted, stored, and ignored - which is
#: the trap config/presets.py describes for `size_mb`.
#:
#: The tape sizes are generated rather than listed, because the densities come
#: from the catalogue and a new one must not need an edit here.
DENSITY_KEYS = tuple(f'{file.TAPE_SIZE}.{d}'
                     for d in personalities.SUFFIX_BY_DENSITY)
KNOWN_KEYS = (file.TAPE_SIZE_DEFAULT,) + DENSITY_KEYS


class SettingsService:
    """The settings file, as answers rather than as TOML."""

    def __init__(self, base=None):
        #: Where the file lives. Tests point this at a temporary directory;
        #: nothing else passes it.
        self.base = base

    @staticmethod
    def canonical(key: str) -> str:
        """The key as the file spells it.

        `tape create --density lto8` works, so `settings set tape.size.lto8`
        has to: a command line that is forgiving about a density in one place
        and strict in another teaches nothing except that it is inconsistent.
        Only the case is forgiven - a key that does not exist is still
        refused, because a setting nothing reads is worse than a missing one.
        """
        known = {k.upper(): k for k in KNOWN_KEYS}
        return known.get((key or '').upper(), key)

    # -- reading -----------------------------------------------------------

    def list(self, section: str = None) -> ServiceResult:
        """Every setting, what it is, and which level of the chain decided it.

        A value that has never been set is still listed, with the default it
        is taking and where that came from, because "what can I change" is
        most of what this is for.

        NARROWING, BECAUSE THIRTY-THREE ROWS IS NOT A LIST
        --------------------------------------------------
        `section` takes any of three things, and case never matters:

            tape, tape.size        a section
            tape.size.lto          a family - every LTO generation
            tape.size.LTO8         one setting

        A family is not always a prefix, which is why this asks
        personalities.media_family rather than matching the string. The 3592
        cartridges are J1A, E05, E06 and E07: they share no prefix with each
        other, being named for the drive generation that writes them, so
        `tape.size.3592` can only work by asking what family each belongs to.
        """
        data = file.read(self.base)
        wanted = (section or '').strip().lower()
        rows = [self._row(key, data) for key in KNOWN_KEYS
                if not wanted or self._matches(key, wanted)]

        if not rows:
            return failure_result(
                f'no settings under {section!r}',
                [f'sections: {", ".join(sorted(self._sections()))}',
                 f'families: {", ".join(self._families())}',
                 'or one setting, as in tape.size.LTO8'])

        changed = [row for row in rows if row['source'] != 'the shipped default']
        said = (f'{len(rows)} setting(s), {len(changed)} changed from the '
                f'shipped default' if changed
                else f'{len(rows)} setting(s), all at the shipped default')
        return success_result(said, {'settings': rows, 'file': str(self.path),
                                     'exists': self.path.exists()})

    def get(self, key: str) -> ServiceResult:
        """One setting, with its value, its source and what it means."""
        key = self.canonical(key)
        if key not in KNOWN_KEYS:
            return self._no_such_key(key)
        row = self._row(key, file.read(self.base))
        return success_result(f'{key} is {row["shown"]} ({row["source"]})', row)

    # -- changing ----------------------------------------------------------

    def set(self, key: str, value) -> ServiceResult:
        """Set one key. Sizes may be written 1000, 2000GB or 12TB."""
        key = self.canonical(key)
        if key not in KNOWN_KEYS:
            return self._no_such_key(key)
        try:
            size = file.parse_size_mb(value)
        except ValueError as problem:
            return failure_result(f'not setting {key}: {problem}',
                                  [str(problem)])

        if not file.write(file.put(key, size, self.base), self.base):
            return self._cannot_write()

        row = self._row(key, file.read(self.base))
        return success_result(f'{key} is now {row["shown"]}', row)

    def reset(self, key: str) -> ServiceResult:
        """Remove one key, so it takes the default again.

        Back to the **shipped** default, not to the native capacity. Native is
        a value somebody chooses, like any other.
        """
        key = self.canonical(key)
        if key not in KNOWN_KEYS:
            return self._no_such_key(key)
        if file.get(key, self.base) is None:
            row = self._row(key, file.read(self.base))
            return success_result(
                f'{key} was not set; it is {row["shown"]} ({row["source"]})',
                row)

        if not file.write(file.drop(key, self.base), self.base):
            return self._cannot_write()

        row = self._row(key, file.read(self.base))
        return success_result(f'{key} is back to {row["shown"]} '
                              f'({row["source"]})', row)

    def apply(self, values: Dict[str, str]) -> ServiceResult:
        """Save a whole form: ``{key: typed}``, where empty means "not set".

        The page submits every row, so this has to tell the three cases apart
        - set, cleared, unchanged - rather than writing whatever it was given.
        Writing all of them would turn a page visit into 33 explicit settings,
        and "I changed one row" would leave a file saying the operator had
        chosen every default by hand.

        One write at the end, not one per row: a page that saved thirty-three
        times would be thirty-three chances to half-fail.
        """
        data = file.read(self.base)
        changed, cleared, refused = [], [], []

        for raw_key, typed in values.items():
            key = self.canonical(raw_key)
            if key not in KNOWN_KEYS:
                refused.append(f'no setting called {raw_key}')
                continue

            current = file.get(key, data=data)
            text = (typed or '').strip()
            if not text:
                if current is not None:
                    data = file.drop(key, data=data)
                    cleared.append(key)
                continue

            try:
                size = file.parse_size_mb(text)
            except ValueError as problem:
                refused.append(str(problem))
                continue
            if size != current:
                data = file.put(key, size, data=data)
                changed.append(key)

        if refused:
            return failure_result(
                f'{len(refused)} setting(s) not saved', refused)
        if not changed and not cleared:
            return success_result('nothing to change', {'changed': [],
                                                        'cleared': []})
        if not file.write(data, self.base):
            return self._cannot_write()

        said = ', '.join(
            part for part in
            (f'{len(changed)} changed' if changed else '',
             f'{len(cleared)} back to the default' if cleared else '')
            if part)
        return success_result(said, {'changed': changed, 'cleared': cleared})

    # -- the shape every verb returns --------------------------------------

    @property
    def path(self):
        return file.settings_path(self.base)

    def _row(self, key: str, data: Dict) -> Dict:
        """One setting as every caller wants it: value, provenance, meaning."""
        density = key.rsplit('.', 1)[-1]
        is_default = key == file.TAPE_SIZE_DEFAULT

        if is_default:
            value = file.tape_size_mb('', self.base, data=data)
            # Not "the file" merely because the file holds it: `render` pins
            # `default` on every write whether or not anybody chose it, so
            # the row would claim a choice nobody made. Same reasoning as
            # config.settings.source_of, which this has to agree with - they
            # are two halves of one column.
            source = ('the file'
                      if value != file.DEFAULT_TAPE_SIZE_MB
                      else 'the shipped default')
            native = None
        else:
            value = file.tape_size_mb(density, self.base, data=data)
            source = file.source_of(density, self.base, data=data)
            native = personalities.NATIVE_CAPACITY_GB.get(density)
            native = native * 1000 if native else None

        return {
            'key': key,
            'value': value,
            'shown': file.as_size(value),
            'source': source,
            'set_here': file.get(key, data=data) is not None,
            'native_mb': native,
            'native_shown': file.as_size(native) if native else None,
        }

    @staticmethod
    def _matches(key: str, wanted: str) -> bool:
        """Does this key belong under what was asked for?

        A prefix would do for LTO and 9840 and get 3592 wrong, so the family
        is asked for rather than inferred from the spelling.
        """
        low = key.lower()
        if low == wanted or low.startswith(wanted + '.') or wanted in (
                'tape', 'tape.size'):
            return True
        # tape.size.lto, tape.size.3592 - the family of this key's density.
        head, _, density = key.rpartition('.')
        return (f'{head}.{personalities.media_family(density)}'.lower()
                == wanted)

    def _sections(self) -> List[str]:
        return {key.rsplit('.', 1)[0] for key in KNOWN_KEYS} | {'tape'}

    @staticmethod
    def _families() -> List[str]:
        """The families there are, in catalogue order, for a refusal to name.

        Built from the densities rather than listed, so a cartridge added to
        the catalogue brings its family with it.
        """
        seen = []
        for key in DENSITY_KEYS:
            family = personalities.media_family(key.rpartition('.')[2])
            if family not in seen:
                seen.append(family)
        return seen

    def _no_such_key(self, key: str) -> ServiceResult:
        """A key nothing reads must be refused rather than stored.

        presets learnt this the other way round: `size_mb` was parsed, stored,
        validated and then ignored, because the thing that created a library
        read a different name. A setting that is accepted and does nothing is
        a worse answer than "no such setting".
        """
        close = [known for known in KNOWN_KEYS
                 if known.rsplit('.', 1)[-1].upper() == key.rsplit('.', 1)[-1].upper()]
        hints = [f'did you mean {close[0]}?'] if close else []
        hints.append(f'`mhvtl settings list` shows every one '
                     f'({len(KNOWN_KEYS)} of them)')
        return failure_result(f'no setting called {key}', hints)

    def _cannot_write(self) -> ServiceResult:
        return failure_result(
            f'cannot write {self.path}',
            [f'{self.path.parent} is not writable by this user',
             'changing a setting needs membership of the mhvtl group, '
             'or sudo'])
