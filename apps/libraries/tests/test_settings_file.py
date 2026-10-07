"""/etc/mhvtl-gui/settings.toml: the console's own preferences.

Every test here points the path at a temporary directory, so the host's own
settings file is never read or written - the same discipline test_presets.py
follows for presets.toml.

What is being guarded is the chain. A cartridge's size is decided at one of
four levels and the whole point of this file is that they cannot disagree:

    1. the code             1,000 MB
    2. tape.size.default    this file, every density
    3. tape.size.<density>  this file, one density
    4. --size-mb            one cartridge, once (not this module's job)

The bug this replaced had two numbers at level 1 and nobody arbitrating, which
is how a library's own tapes came to be 500 MB while any tape added to it
afterwards was 500 GB.
"""
import re
import tomllib
from pathlib import Path

from .base import SimpleTestCase

from apps.libraries.services.config import settings
from apps.libraries.services.profiles import personalities


class ChainTests(SimpleTestCase):
    """Which of the four levels answers, and what it says."""

    def setUp(self):
        self.base = Path(self.tmpdir())

    def path(self):
        return settings.settings_path(self.base)

    # -- level 1, the code -------------------------------------------------

    def test_with_no_file_every_density_is_the_shipped_default(self):
        self.assertEqual(settings.read(self.base), {})
        for density in ('LTO8', 'LTO10', 'AIT4', '9840C'):
            with self.subTest(density=density):
                self.assertEqual(settings.tape_size_mb(density, self.base),
                                 settings.DEFAULT_TAPE_SIZE_MB)

    def test_the_shipped_default_is_a_thousand(self):
        """1,000 rather than 1,024: tapes/service.UNKNOWN_SIZE_MB is already
        1,000, so the console has one idea of a gigabyte."""
        self.assertEqual(settings.DEFAULT_TAPE_SIZE_MB, 1000)

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(self.path().exists())
        self.assertEqual(settings.read(self.base), {})
        self.assertEqual(settings.source_of('LTO8', self.base),
                         'the shipped default')

    def test_an_unreadable_file_falls_back_rather_than_raising(self):
        """A settings page that cannot render is worse than one showing the
        defaults, so a corrupt file costs the overrides and nothing else."""
        self.path().write_text('this is not toml = = =\n')
        self.assertEqual(settings.read(self.base), {})
        self.assertEqual(settings.tape_size_mb('LTO8', self.base), 1000)

    # -- level 2, the file's default ---------------------------------------

    def test_the_file_default_covers_every_density(self):
        settings.write(settings.put(settings.TAPE_SIZE_DEFAULT, 2000,
                                    self.base), self.base)
        for density in ('LTO8', 'AIT4', '9840C'):
            with self.subTest(density=density):
                self.assertEqual(settings.tape_size_mb(density, self.base), 2000)
        self.assertEqual(settings.source_of('LTO8', self.base),
                         'the file default')

    # -- level 3, one density ----------------------------------------------

    def test_a_density_overrides_the_file_default(self):
        data = settings.put(settings.TAPE_SIZE_DEFAULT, 2000, self.base)
        settings.write(data, self.base)
        settings.write(settings.put('tape.size.LTO8', 12_000_000, self.base),
                       self.base)

        self.assertEqual(settings.tape_size_mb('LTO8', self.base), 12_000_000)
        self.assertEqual(settings.tape_size_mb('LTO9', self.base), 2000)
        self.assertEqual(settings.source_of('LTO8', self.base),
                         'set for this density')
        self.assertEqual(settings.source_of('LTO9', self.base),
                         'the file default')

    def test_the_density_is_matched_whatever_case_it_is_asked_in(self):
        settings.write(settings.put('tape.size.LTO8', 777, self.base), self.base)
        self.assertEqual(settings.tape_size_mb('lto8', self.base), 777)

    def test_a_nonsense_value_is_ignored_rather_than_used(self):
        """Hand-edited files happen. A size of zero or a string is not a
        capacity, and falling through to the default beats creating a
        cartridge of nothing."""
        for bad in (0, -5, 'big', None):
            with self.subTest(value=bad):
                settings.write(settings.put('tape.size.LTO8', bad, self.base),
                               self.base)
                self.assertEqual(settings.tape_size_mb('LTO8', self.base), 1000)

    # -- reset -------------------------------------------------------------

    def test_reset_returns_a_density_to_the_default_not_to_native(self):
        """`reset` means back to the shipped default. Native capacity is a
        value you choose, like any other."""
        settings.write(settings.put('tape.size.LTO8', 12_000_000, self.base),
                       self.base)
        settings.write(settings.drop('tape.size.LTO8', self.base), self.base)

        self.assertEqual(settings.tape_size_mb('LTO8', self.base), 1000)
        self.assertNotIn('LTO8', settings.read(self.base)['tape']['size'])

    def test_dropping_a_key_that_is_not_there_changes_nothing(self):
        settings.write(settings.put('tape.size.LTO8', 500, self.base), self.base)
        before = settings.read(self.base)
        settings.drop('tape.size.LTO9', self.base)
        self.assertEqual(settings.read(self.base), before)


class FileFormatTests(SimpleTestCase):
    """What gets written, and whether it can be read back."""

    def setUp(self):
        self.base = Path(self.tmpdir())

    def written(self):
        return settings.settings_path(self.base).read_text()

    def test_what_it_writes_is_valid_toml_it_can_read_again(self):
        settings.write(settings.put('tape.size.LTO8', 12_000_000, self.base),
                       self.base)
        parsed = tomllib.loads(self.written())
        self.assertEqual(parsed['tape']['size']['LTO8'], 12_000_000)
        self.assertEqual(settings.read(self.base), parsed)

    def test_the_native_capacity_is_written_beside_each_density(self):
        """The reference an operator needs to put a value back without
        reading the documentation."""
        settings.write(settings.put('tape.size.LTO8', 1000, self.base),
                       self.base)
        native = personalities.NATIVE_CAPACITY_GB['LTO8'] * 1000
        self.assertIn(f'# native {native:,} MB', self.written())

    def test_the_comment_is_regenerated_rather_than_kept(self):
        """Editing a value must not orphan the note beside it."""
        settings.write(settings.put('tape.size.LTO8', 1000, self.base),
                       self.base)
        settings.write(settings.put('tape.size.LTO8', 55, self.base), self.base)
        text = self.written()
        self.assertEqual(text.count('# native'), 1)
        self.assertIn('LTO8 = 55', text)

    def test_a_density_with_no_native_capacity_says_so(self):
        settings.write(settings.put('tape.size.9840C', 1000, self.base),
                       self.base)
        self.assertIn('# no native capacity', self.written())

    def test_the_default_is_always_written_so_the_knob_is_visible(self):
        """And so that changing the shipped default in a later release cannot
        silently move a host that already has a file."""
        settings.write(settings.put('tape.size.LTO8', 1000, self.base),
                       self.base)
        self.assertIn('default = 1000', self.written())

    def test_the_header_explains_the_file_to_someone_who_opened_it(self):
        settings.write({}, self.base)
        text = self.written()
        self.assertIn('mhvtl settings set', text)
        self.assertIn('MB', text)

    def test_a_future_section_round_trips(self):
        """The file is built to hold more than tape sizes - a default drive,
        say - and a second section must survive a write of the first."""
        settings.write(settings.put('drive.default', 'ULT3580-TDA', self.base),
                       self.base)
        settings.write(settings.put('tape.size.LTO8', 1000, self.base),
                       self.base)
        self.assertEqual(settings.get('drive.default', self.base),
                         'ULT3580-TDA')


class DottedKeyTests(SimpleTestCase):
    """The key is the path through the file, so what you type is what you
    find - the rule presets already follows for its keys."""

    def setUp(self):
        self.base = Path(self.tmpdir())

    def test_get_walks_the_dots(self):
        settings.write(settings.put('tape.size.LTO8', 42, self.base), self.base)
        self.assertEqual(settings.get('tape.size.LTO8', self.base), 42)

    def test_a_key_that_is_not_there_is_none_not_an_error(self):
        self.assertIsNone(settings.get('tape.size.LTO8', self.base))
        self.assertIsNone(settings.get('nothing.like.this', self.base))

    def test_a_section_is_not_a_value(self):
        settings.write(settings.put('tape.size.LTO8', 42, self.base), self.base)
        self.assertIsNone(settings.get('tape.size', self.base))


class ShippedExampleTests(SimpleTestCase):
    """packaging/settings.toml.example.

    Nothing reads it - the console reads settings.toml and an upgrade replaces
    this - which is exactly why it is tested. An example naming a capacity
    that is not the cartridge's, or one that does not parse, teaches the wrong
    thing to the one person who opened the file instead of the documentation.
    """

    EXAMPLE = Path(__file__).resolve().parents[3] / 'packaging/settings.toml.example'

    def setUp(self):
        self.text = self.EXAMPLE.read_text()

    def test_it_parses_with_the_parser_that_will_read_it(self):
        parsed = tomllib.loads(self.text)
        self.assertEqual(parsed['tape']['size']['default'],
                         settings.DEFAULT_TAPE_SIZE_MB)

    def test_only_the_default_is_live_and_the_rest_are_commented(self):
        """A shipped file that set every density would be an operator's
        choices nobody made."""
        parsed = tomllib.loads(self.text)
        self.assertEqual(list(parsed['tape']['size']), ['default'])

    def test_every_density_is_offered(self):
        """A density missing from the example is one somebody has to find out
        about from the source."""
        named = set(re.findall(r'#\s+(\w+)\s+= \d+', self.text))
        for density in personalities.SUFFIX_BY_DENSITY:
            with self.subTest(density=density):
                self.assertIn(density, named)

    def test_every_capacity_it_quotes_is_the_real_one(self):
        """The comments are the reference for putting a value back, so a
        wrong one is worse than none."""
        wrong = []
        for density, shown in re.findall(r'#\s+(\w+)\s+= \d+\s+# native ([\d,]+) MB',
                                         self.text):
            capacity = personalities.NATIVE_CAPACITY_GB.get(density)
            if not capacity or f'{capacity * 1000:,}' != shown:
                wrong.append(f'{density} says {shown}')
        self.assertEqual(wrong, [], '; '.join(wrong))

    def test_it_is_packaged_as_an_example_and_never_as_the_live_file(self):
        """An upgrade replaces it. Landing as settings.toml would overwrite
        what an operator had chosen."""
        spec = (Path(__file__).resolve().parents[3]
                / 'packaging/rpm/mhvtl-gui.spec').read_text()
        self.assertIn('install -m 644 packaging/settings.toml.example', spec)
        for line in spec.splitlines():
            if line.strip().endswith('/etc/mhvtl-gui/settings.toml'):
                self.fail(f'the spec installs the live file: {line.strip()}')

    def test_the_build_puts_it_in_the_source_tarball(self):
        """The spec installs it out of packaging/, so build.sh has to copy it
        in - a file in one and not the other fails at rpmbuild time, on
        whichever machine builds the release."""
        build = (Path(__file__).resolve().parents[3]
                 / 'packaging/build.sh').read_text()
        self.assertIn('settings.toml.example', build)


class ThePinIsNotAChoiceTests(SimpleTestCase):
    """What the provenance column says about a default nobody set.

    `render` writes `default` on every write whether or not anybody asked
    for it, so that changing the shipped default in a later release cannot
    silently resize a host that already has a file. That is deliberate.

    What it must not do is report itself as a decision. Setting one density
    used to make every other density read "the file default", and the `list`
    summary then said eleven settings had changed when one had - in the
    column the whole feature exists to fill in.
    """

    def setUp(self):
        self.base = Path(self.tmpdir())

    def service(self):
        from apps.libraries.services.settings import SettingsService
        return SettingsService(self.base)

    def test_setting_one_density_leaves_the_others_at_the_shipped_default(self):
        self.service().set('tape.size.LTO8', '12TB')
        self.assertEqual(settings.source_of('LTO9', self.base),
                         'the shipped default')
        self.assertEqual(settings.source_of('LTO8', self.base),
                         'set for this density')

    def test_the_pin_is_still_written(self):
        """The behaviour it protects is unchanged; only the wording moved."""
        self.service().set('tape.size.LTO8', '12TB')
        self.assertIn('default = 1000',
                      settings.settings_path(self.base).read_text())

    def test_one_change_is_reported_as_one(self):
        said = self.service().list('tape.size.lto').message
        self.assertIn('all at the shipped default', said)

        self.service().set('tape.size.LTO8', '12TB')
        said = self.service().list('tape.size.lto').message
        self.assertIn('1 changed', said)

    def test_a_default_somebody_chose_is_still_the_file_s(self):
        """The other half: a default that differs from the shipped one is a
        choice, covers every density, and must say so."""
        self.service().set('tape.size.default', '5000')
        self.assertEqual(settings.source_of('LTO9', self.base),
                         'the file default')
        self.assertIn('11 changed',
                      self.service().list('tape.size.lto').message)

    def test_the_default_s_own_row_agrees_with_the_densities(self):
        """Two functions fill one column - source_of for a density and
        SettingsService._row for `tape.size.default` - and they have to give
        the same answer about the same file."""
        self.service().set('tape.size.LTO8', '12TB')
        row = self.service().get(settings.TAPE_SIZE_DEFAULT).data
        self.assertEqual(row['source'], 'the shipped default')

        self.service().set('tape.size.default', '5000')
        row = self.service().get(settings.TAPE_SIZE_DEFAULT).data
        self.assertEqual(row['source'], 'the file')

    def test_a_default_set_to_the_shipped_value_reads_as_shipped(self):
        """Deliberately indistinguishable. Someone who types the shipped
        number has chosen nothing the console was not already doing, and a
        column saying otherwise would be a difference without a consequence.
        """
        self.service().set('tape.size.default', '1000')
        self.assertEqual(settings.source_of('LTO9', self.base),
                         'the shipped default')
