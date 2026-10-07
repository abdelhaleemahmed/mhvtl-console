"""`mhvtl settings` and the service behind it.

The settings page and this command call one service, so the two cannot answer
differently - which is the same reason the tape pages and `mhvtl tape` share
one. What is guarded here is mostly refusals: a setting that is accepted and
then ignored is the trap config/presets.py describes for `size_mb`, and it is
worse than one that does not exist.

Every test points MHVTL_GUI_CONFIG_DIR at a temporary directory, so the host's
own /etc/mhvtl-gui/settings.toml is never read or written.
"""
import json
from pathlib import Path
from unittest import mock

from django.test import override_settings

from .base import SimpleTestCase, TestCase
from .test_cli import run

from apps.libraries.services.config import settings as file
from apps.libraries.services.settings import SettingsService
from apps.libraries.services.settings.service import KNOWN_KEYS
from mhvtl_cli import privileges


class ServiceTests(SimpleTestCase):

    def setUp(self):
        self.base = Path(self.tmpdir())
        self.service = SettingsService(self.base)

    # -- list --------------------------------------------------------------

    def test_list_shows_a_setting_that_has_never_been_set(self):
        """"What can I change" is most of what this is for, so a key with no
        value is still a row, with the default it is taking."""
        result = self.service.list()
        self.assertTrue(result.success)
        keys = [row['key'] for row in result.data['settings']]
        self.assertIn('tape.size.LTO8', keys)
        self.assertIn('tape.size.default', keys)
        self.assertFalse(result.data['exists'])

    def test_every_row_says_where_its_value_came_from(self):
        for row in self.service.list().data['settings']:
            with self.subTest(key=row['key']):
                self.assertEqual(row['source'], 'the shipped default')
                self.assertEqual(row['value'], file.DEFAULT_TAPE_SIZE_MB)

    def test_a_row_says_what_the_cartridge_really_holds(self):
        row = next(r for r in self.service.list().data['settings']
                   if r['key'] == 'tape.size.LTO8')
        self.assertEqual(row['native_mb'], 12_000_000)
        self.assertNotEqual(row['native_mb'], row['value'])

    def test_list_narrows_to_a_section(self):
        rows = self.service.list('tape.size').data['settings']
        self.assertTrue(all(r['key'].startswith('tape.size') for r in rows))

    def test_a_section_with_nothing_in_it_is_refused_with_the_ones_there_are(self):
        result = self.service.list('nothing')
        self.assertFalse(result.success)
        self.assertIn('tape', ' '.join(result.errors))

    # -- set ---------------------------------------------------------------

    def test_set_accepts_the_units_a_person_types(self):
        for typed, expected in (('1000', 1000), ('2000GB', 2_000_000),
                                ('12TB', 12_000_000), ('2.5TB', 2_500_000)):
            with self.subTest(typed=typed):
                result = self.service.set('tape.size.LTO8', typed)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(result.data['value'], expected)

    def test_set_refuses_binary_units_and_says_why(self):
        """12 TB and 12 TiB differ by ten per cent, in a table whose purpose
        is matching real hardware."""
        result = self.service.set('tape.size.LTO8', '12TiB')
        self.assertFalse(result.success)
        self.assertIn('decimal', result.message)

    def test_set_refuses_a_key_nothing_reads(self):
        result = self.service.set('tape.size.LTO99', '1000')
        self.assertFalse(result.success)
        self.assertIn('no setting called', result.message)

    def test_set_refuses_something_that_is_not_a_size(self):
        for bad in ('nonsense', '0', '-5', ''):
            with self.subTest(value=bad):
                self.assertFalse(self.service.set('tape.size.LTO8', bad).success)

    def test_a_key_is_forgiven_its_case_like_a_density_everywhere_else(self):
        """`tape create --density lto8` works, so this has to."""
        result = self.service.set('tape.size.lto8', '2TB')
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.data['key'], 'tape.size.LTO8')

    # -- reset -------------------------------------------------------------

    def test_reset_goes_to_the_shipped_default_not_to_native(self):
        self.service.set('tape.size.LTO8', '12TB')
        result = self.service.reset('tape.size.LTO8')
        self.assertTrue(result.success)
        self.assertEqual(result.data['value'], file.DEFAULT_TAPE_SIZE_MB)
        self.assertNotEqual(result.data['value'], 12_000_000)

    def test_resetting_something_never_set_says_so_rather_than_failing(self):
        result = self.service.reset('tape.size.LTO8')
        self.assertTrue(result.success)
        self.assertIn('was not set', result.message)

    def test_reset_refuses_a_key_nothing_reads(self):
        self.assertFalse(self.service.reset('tape.size.LTO99').success)

    # -- the file ----------------------------------------------------------

    def test_nothing_is_written_until_something_is_set(self):
        self.service.list()
        self.service.get('tape.size.LTO8')
        self.assertFalse(self.service.path.exists())

    def test_a_write_that_cannot_land_is_reported_not_raised(self):
        with mock.patch.object(file, 'write', return_value=False):
            result = self.service.set('tape.size.LTO8', '12TB')
        self.assertFalse(result.success)
        self.assertIn('mhvtl group', ' '.join(result.errors))

    def test_every_known_key_can_be_read(self):
        """A key in KNOWN_KEYS that `get` refuses would be a setting the page
        offers and the command cannot answer for."""
        for key in KNOWN_KEYS:
            with self.subTest(key=key):
                self.assertTrue(self.service.get(key).success)


class CommandTests(TestCase):
    """The verbs, through the real parser."""

    def setUp(self):
        self.base = self.tmpdir()
        override = override_settings(MHVTL_GUI_CONFIG_DIR=str(self.base))
        override.enable()
        self.addCleanup(override.disable)
        granted = mock.patch.object(privileges, 'can_write', return_value=True)
        granted.start()
        self.addCleanup(granted.stop)

    def test_list_prints_a_row_per_setting_with_its_source(self):
        code, out, err = run(['settings', 'list'])
        self.assertEqual(code, 0)
        self.assertIn('tape.size.LTO8', out)
        self.assertIn('the shipped default', out)
        self.assertIn('12 TB', out, 'the native capacity is not shown')

    def test_list_says_there_is_no_file_yet(self):
        """A reader who has just seen a table will otherwise go looking for
        the file those values came out of."""
        code, out, err = run(['settings', 'list'])
        self.assertIn('No settings file yet', out)

    def test_set_then_get_round_trips(self):
        code, out, err = run(['settings', 'set', 'tape.size.LTO8', '12TB'])
        self.assertEqual(code, 0, err)
        self.assertIn('12 TB', out)

        code, out, err = run(['settings', 'get', 'tape.size.LTO8'])
        self.assertEqual(code, 0)
        self.assertIn('12 TB', out)
        self.assertIn('set for this density', out)

    def test_set_says_it_applies_to_tapes_made_from_now_on(self):
        code, out, err = run(['settings', 'set', 'tape.size.LTO8', '12TB'])
        self.assertIn('from now on', out)
        self.assertIn('keep the size they were made with', out)

    def test_reset_returns_it_to_the_default(self):
        run(['settings', 'set', 'tape.size.LTO8', '12TB'])
        code, out, err = run(['settings', 'reset', 'tape.size.LTO8'])
        self.assertEqual(code, 0, err)
        self.assertIn('1 GB', out)

    def test_a_bad_key_is_refused_with_a_way_forward(self):
        code, out, err = run(['settings', 'set', 'tape.size.LTO99', '1000'])
        self.assertNotEqual(code, 0)
        self.assertIn('settings list', err)

    def test_json_carries_the_value_and_the_provenance(self):
        run(['settings', 'set', 'tape.size.LTO8', '12TB'])
        code, out, err = run(['--json', 'settings', 'get', 'tape.size.LTO8'])
        payload = json.loads(out)['data']
        self.assertEqual(payload['value'], 12_000_000)
        self.assertEqual(payload['native_mb'], 12_000_000)
        self.assertEqual(payload['source'], 'set for this density')

    def test_changing_a_setting_needs_the_group(self):
        """Every other mutation does; this writes to /etc/mhvtl-gui."""
        with mock.patch.object(privileges, 'can_write', return_value=False):
            code, out, err = run(['settings', 'set', 'tape.size.LTO8', '12TB'])
        self.assertNotEqual(code, 0)
        self.assertIn('mhvtl', err)

    def test_reading_needs_nothing(self):
        with mock.patch.object(privileges, 'can_write', return_value=False):
            code, out, err = run(['settings', 'list'])
        self.assertEqual(code, 0)


class PageTests(TestCase):
    """The Settings page, which calls the same service the command does."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.base = self.tmpdir()
        override = override_settings(MHVTL_GUI_CONFIG_DIR=str(self.base))
        override.enable()
        self.addCleanup(override.disable)

        user = get_user_model().objects.create_user('settings', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()
        self.url = '/libraries/console/settings/'

    def test_it_lists_every_density_with_what_it_will_be_made_at(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn('LTO8', body)
        self.assertIn('12 TB', body, 'the native capacity is not offered')
        self.assertIn('the shipped default', body)

    def test_it_says_there_is_no_file_yet(self):
        body = self.client.get(self.url).content.decode()
        self.assertIn('not written yet', body)

    def test_saving_a_value_writes_the_file_and_changes_what_a_create_does(self):
        from apps.libraries.services.tapes import service as tapes

        self.client.post(self.url, {'tape.size.LTO8': '12TB'}, follow=True)
        self.assertEqual(tapes.size_for('LTO8'), 12_000_000)
        self.assertEqual(tapes.size_for('LTO9'), file.DEFAULT_TAPE_SIZE_MB)

    def test_an_empty_field_means_not_set_rather_than_zero(self):
        """Which is how the page offers what `settings reset` offers, without
        a button per row."""
        from apps.libraries.services.tapes import service as tapes

        self.client.post(self.url, {'tape.size.LTO8': '12TB'}, follow=True)
        self.client.post(self.url, {'tape.size.LTO8': ''}, follow=True)
        self.assertEqual(tapes.size_for('LTO8'), file.DEFAULT_TAPE_SIZE_MB)

    def test_a_bad_value_is_refused_and_nothing_is_written(self):
        page = self.client.post(self.url, {'tape.size.LTO8': 'nonsense'},
                                follow=True)
        self.assertFalse(SettingsService(self.base).path.exists())
        self.assertContains(page, 'not a size')

    def test_the_page_and_the_command_agree(self):
        """They call one service; this is the assertion that says so."""
        self.client.post(self.url, {'tape.size.LTO8': '2TB'}, follow=True)
        code, out, err = run(['settings', 'get', 'tape.size.LTO8'])
        self.assertEqual(code, 0)
        self.assertIn('2 TB', out)


class NarrowingTests(SimpleTestCase):
    """Thirty-three rows is not a list anybody reads."""

    def setUp(self):
        self.service = SettingsService(Path(self.tmpdir()))

    def rows(self, section=None):
        result = self.service.list(section)
        return [row['key'] for row in result.data['settings']] if result.success else []

    def test_everything_without_a_section(self):
        self.assertEqual(len(self.rows()), len(KNOWN_KEYS))

    def test_a_section(self):
        self.assertEqual(len(self.rows('tape.size')), len(KNOWN_KEYS))
        self.assertEqual(len(self.rows('tape')), len(KNOWN_KEYS))

    def test_a_family(self):
        lto = self.rows('tape.size.lto')
        self.assertTrue(lto)
        self.assertTrue(all('LTO' in key for key in lto))
        self.assertNotIn('tape.size.AIT4', lto)

    def test_a_family_that_is_not_a_prefix(self):
        """J1A, E05, E06 and E07 share no prefix with each other: they are
        named for the drive generation that writes them. Matching the spelling
        would miss every one of them."""
        self.assertEqual(sorted(self.rows('tape.size.3592')),
                         ['tape.size.E05', 'tape.size.E06', 'tape.size.E07',
                          'tape.size.J1A'])

    def test_sdlt_does_not_drag_in_dlt(self):
        self.assertEqual(self.rows('tape.size.dlt'), ['tape.size.DLT4'])

    def test_one_key(self):
        self.assertEqual(self.rows('tape.size.LTO8'), ['tape.size.LTO8'])

    def test_case_never_matters(self):
        self.assertEqual(self.rows('TAPE.SIZE.LTO'), self.rows('tape.size.lto'))
        self.assertEqual(self.rows('tape.size.lto8'), ['tape.size.LTO8'])

    def test_nothing_matching_says_what_can_be_asked_for(self):
        refused = self.service.list('tape.size.nope')
        self.assertFalse(refused.success)
        hints = ' '.join(refused.errors)
        self.assertIn('families', hints)
        self.assertIn('3592', hints)
        self.assertIn('tape.size.LTO8', hints)


class DocumentedKeysExistTests(SimpleTestCase):
    """Every settings key this file's own help shows has to be real.

    SuggestedCommandTests checks that a documented `mhvtl <noun> <verb>`
    parses, and that is not enough here: `settings set drive.default
    ULT3580-TDA` parses perfectly and then fails, because `set` refuses a key
    nothing reads. That exact line sat in this module's docstring as the
    illustration of how a future setting would work - the one command in the
    file that could not run.
    """

    SOURCES = ('mhvtl_cli/commands/settings.py',
               'apps/libraries/services/settings/service.py')

    def test_no_documentation_names_a_key_that_does_not_exist(self):
        import re
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[3]
        pattern = re.compile(r'settings (?:set|get|reset)\s+([\w.]+)')
        wrong = []
        for name in self.SOURCES:
            for key in pattern.findall((root / name).read_text()):
                if SettingsService.canonical(key) not in KNOWN_KEYS:
                    wrong.append(f'{name}: {key}')
        self.assertEqual(wrong, [], '; '.join(wrong))

    def test_the_check_would_notice_one(self):
        """A test that cannot fail protects nothing."""
        self.assertNotIn(SettingsService.canonical('drive.default'), KNOWN_KEYS)


class CaseNeverMattersTests(SimpleTestCase):
    """Every verb, every shape of key, any case.

    `tape create --density lto8` works, so nothing here may be stricter. This
    is pinned verb by verb rather than once, because the normalising happens
    in two places - canonical() for a single key and the filter for a section
    - and one could be fixed while the other rots.
    """

    def setUp(self):
        self.service = SettingsService(Path(self.tmpdir()))

    def test_list_narrows_the_same_whatever_the_case(self):
        for spelling in ('tape.size.LTO', 'tape.size.lto', 'tape.size.Lto',
                         'TAPE.SIZE.LTO'):
            with self.subTest(spelling=spelling):
                rows = [r['key'] for r in self.service.list(spelling).data['settings']]
                self.assertEqual(len(rows), 11)

    def test_a_section_in_any_case(self):
        for spelling in ('tape', 'TAPE', 'Tape.Size', 'tape.size'):
            with self.subTest(spelling=spelling):
                self.assertTrue(self.service.list(spelling).success)

    def test_set_get_and_reset_take_any_case_and_answer_canonically(self):
        self.assertTrue(self.service.set('tape.size.lto8', '12TB').success)
        got = self.service.get('TAPE.SIZE.LTO8')
        self.assertTrue(got.success)
        self.assertEqual(got.data['key'], 'tape.size.LTO8')
        self.assertEqual(got.data['value'], 12_000_000)

        reset = self.service.reset('Tape.Size.Lto8')
        self.assertTrue(reset.success)
        self.assertEqual(reset.data['key'], 'tape.size.LTO8')

    def test_a_family_in_any_case(self):
        for spelling in ('tape.size.3592', 'tape.size.T10000',
                         'tape.size.t10000', 'tape.size.AIT', 'tape.size.ait'):
            with self.subTest(spelling=spelling):
                self.assertTrue(self.service.list(spelling).success,
                                f'{spelling} matched nothing')

    def test_the_file_is_written_in_one_spelling_whatever_was_typed(self):
        """Two spellings of one density in the file would be two settings,
        and the second would silently lose to the first."""
        self.service.set('tape.size.lto8', '12TB')
        self.service.set('TAPE.SIZE.LTO8', '2TB')
        sizes = file.read(self.service.base)['tape']['size']
        self.assertEqual([k for k in sizes if k != 'default'], ['LTO8'])
        self.assertEqual(sizes['LTO8'], 2_000_000)
