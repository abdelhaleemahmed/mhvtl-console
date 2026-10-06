"""The vendor catalogues: the composition, and the two verbs over it.

`mhvtl profile list` and `mhvtl profile show` replaced `mhvtl drive models` on
4 October 2026, which was wrong twice over: it put the nine-profile summary
under the `drive` noun, and nothing at all could list a profile's *library*
models, so the only way to see them was to name one wrong and read the
refusal.

What is tested here is mostly the composition in services/profiles/catalogue,
because that is the part two front ends share. Three callers had each written
it out - the CLI, the web's vendor page, and very nearly the interactive
create - and the point of the module is that they cannot drift.

Nothing here reads device.conf or touches a host: a catalogue is what could be
asked for, before any library exists.
"""
from django.test import SimpleTestCase as DjangoSimpleTestCase

from .base import SimpleTestCase, TestCase
from .test_cli import run

from apps.libraries.services.profiles import catalogue, data

#: Vendors whose drives stop at Ultrium 8, measured rather than assumed: HP
#: makes no LTO-9 drive, so it is not an answer to "I want an LTO-9 library"
#: even though six of the nine profiles are LTO families.
NO_LTO9 = 'HP'


class WritesTests(SimpleTestCase):
    """A density a drive loads is not a density it can write."""

    def test_a_read_only_generation_is_not_written(self):
        """An IBM TD3 writes LTO3 and LTO2 and only reads LTO1. Offering it
        for an LTO1 library would hand somebody cartridges they can restore
        from and never back up to."""
        self.assertEqual(catalogue.writes('ULT3580-TD3'), ['LTO3', 'LTO2'])
        self.assertIn('LTO1', data.creatable_media('ULT3580-TD3'))
        self.assertEqual(data.read_only_media('ULT3580-TD3'), ['LTO1'])

    def test_native_first(self):
        self.assertEqual(catalogue.writes('ULT3580-TD8')[0], 'LTO8')


class NamesTests(SimpleTestCase):

    def test_every_profile_with_no_filter(self):
        self.assertEqual(catalogue.names(), list(data.list_profiles()))

    def test_only_the_profiles_that_write_the_density(self):
        """The question names alone cannot answer, and the one that sends
        people to the wrong vendor: STK is T10000, SONY is AIT, QUANTUM is
        SDLT."""
        self.assertEqual(catalogue.names(media='T10KC'), ['STK'])
        self.assertEqual(catalogue.names(media='AIT4'), ['SONY'])
        lto9 = catalogue.names(media='LTO9')
        self.assertIn('IBM', lto9)
        self.assertNotIn(NO_LTO9, lto9)

    def test_the_density_is_matched_whatever_its_case(self):
        self.assertEqual(catalogue.names(media='t10kc'), ['STK'])

    def test_a_density_nobody_writes_is_no_profiles_not_an_error(self):
        """`profile list --media NOSUCH` is a question with a real answer -
        none - and the CLI turns that into the refusal."""
        self.assertEqual(catalogue.names(media='NOSUCH'), [])


class SummariesTests(SimpleTestCase):

    def test_one_row_per_profile(self):
        rows = catalogue.summaries()
        self.assertEqual([row['profile'] for row in rows],
                         list(data.list_profiles()))

    def test_the_defaults_are_the_ones_a_bare_profile_would_choose(self):
        """The whole point of the row: what `--profile IBM` alone does."""
        for row in catalogue.summaries():
            defaults = data.get_profile_options(row['profile'])['defaults']
            with self.subTest(profile=row['profile']):
                self.assertEqual(row['default_model'],
                                 defaults['library_model'])
                self.assertEqual(row['default_drive'], defaults['drive_model'])
                self.assertEqual(row['default_media'], defaults['media_type'])

    def test_the_counts_match_what_describe_lists(self):
        """Two commands counting the same catalogue differently is the bug
        this module exists to make impossible."""
        for row in catalogue.summaries():
            described = catalogue.describe(row['profile'])
            with self.subTest(profile=row['profile']):
                self.assertEqual(row['model_count'], len(described['models']))
                self.assertEqual(row['drive_count'], len(described['drives']))

    def test_the_default_density_tells_the_families_apart(self):
        """The column that does the work: nothing else warns somebody that
        SONY will give them AIT4 cartridges LTFS cannot open."""
        by_key = {row['profile']: row for row in catalogue.summaries()}
        self.assertEqual(by_key['SONY']['default_media'], 'AIT4')
        self.assertEqual(by_key['STK']['default_media'], 'T10KC')
        self.assertEqual(by_key['QUANTUM']['default_media'], 'SDLT600')
        self.assertEqual(by_key['IBM']['default_media'], 'LTO8')


class DescribeTests(SimpleTestCase):

    def test_every_model_and_drive_with_no_narrowing(self):
        described = catalogue.describe('IBM')
        options = data.get_profile_options('IBM')
        self.assertEqual([row['model'] for row in described['models']],
                         options['library_models'])
        self.assertEqual({row['model'] for row in described['drives']},
                         set(options['drive_models']))

    def test_the_profile_is_matched_whatever_its_case(self):
        """The web reaches a profile through a URL segment (/setup/brand/ibm/)
        and the command line is typed by hand."""
        self.assertEqual(catalogue.describe('ibm')['profile'], 'IBM')
        self.assertEqual(catalogue.describe(' Ibm ')['profile'], 'IBM')

    def test_a_library_model_narrows_the_drives_to_what_it_takes(self):
        everything = catalogue.describe('IBM')
        narrowed = catalogue.describe('IBM', library_model='03584L32')
        self.assertEqual(len(narrowed['models']), 1)
        self.assertLess(len(narrowed['drives']), len(everything['drives']))
        self.assertEqual(
            [row['model'] for row in narrowed['drives']],
            data.get_valid_drives_for_library('IBM', '03584L32'))

    def test_a_density_narrows_both_sections(self):
        """Narrowing beats refusing: only the models that can hold a drive
        which writes LTO9, and only those drives."""
        narrowed = catalogue.describe('IBM', media='LTO9')
        for row in narrowed['drives']:
            with self.subTest(drive=row['model']):
                self.assertIn('LTO9', row['writes'])
        for row in narrowed['models']:
            with self.subTest(model=row['model']):
                self.assertGreater(row['drive_count'], 0)

    def test_a_model_with_nothing_left_is_dropped_not_listed_empty(self):
        """A model listed with no drives under it would read as "this model
        takes the tape", which is the opposite of true."""
        for row in catalogue.describe('STK', media='LTO9')['models']:
            self.assertTrue(row['drives'])

    def test_like_filters_by_name(self):
        narrowed = catalogue.describe('IBM', like='HH')
        self.assertTrue(narrowed['drives'])
        for row in narrowed['drives']:
            self.assertIn('HH', row['model'])

    def test_the_narrowings_compose(self):
        narrowed = catalogue.describe('IBM', library_model='03584L32',
                                      media='LTO9', like='TD')
        self.assertEqual([row['model'] for row in narrowed['drives']],
                         ['ULT3580-TD9'])

    def test_an_unknown_profile_is_refused_with_the_valid_ones(self):
        with self.assertRaises(catalogue.UnknownProfile) as refusal:
            catalogue.describe('NOSUCH')
        self.assertIn('profiles:', refusal.exception.fixes[0])
        self.assertIn('IBM', refusal.exception.fixes[0])

    def test_a_model_the_vendor_does_not_make_is_refused_with_its_models(self):
        with self.assertRaises(catalogue.UnknownProfile) as refusal:
            catalogue.describe('IBM', library_model='SL500')
        self.assertIn('03584L32', refusal.exception.fixes[0])

    def test_a_density_the_vendor_does_not_take_is_refused(self):
        with self.assertRaises(catalogue.UnknownProfile) as refusal:
            catalogue.describe('IBM', media='AIT4')
        self.assertIn('densities:', refusal.exception.fixes[0])


class DefaultDriveTests(SimpleTestCase):
    """Which drive a model gets when nobody chooses one."""

    def test_the_profile_default_when_the_model_takes_it(self):
        self.assertEqual(catalogue.default_drive_for('IBM', '03584L32'),
                         'ULT3580-TD8')

    def test_the_newest_generation_when_it_does_not(self):
        """STK's default is a T10000C and an SL150 cannot carry one."""
        self.assertNotIn('T10000C',
                         data.get_valid_drives_for_library('STK', 'SL150'))
        self.assertEqual(catalogue.default_drive_for('STK', 'SL150'),
                         'ULT3580-TDA')

    def test_a_wanted_density_picks_the_newest_drive_that_writes_it(self):
        self.assertEqual(
            catalogue.default_drive_for('IBM', '03584L32', media='LTO9'),
            'ULT3580-TD9')
        self.assertEqual(
            catalogue.default_drive_for('IBM', '03584L32', media='LTO10'),
            'ULT3580-TDA')

    def test_full_height_beats_half_height_at_the_same_generation(self):
        """Measured in Chromium first: the setup form picked the last drive
        listed among the writers, and IBM lists its half-height drives after
        the full-height ones - so it answered ULT3580-HH9 where the rule
        answers ULT3580-TD9. Same generation, not the drive anybody means."""
        writers = [drive for drive
                   in data.get_valid_drives_for_library('IBM', '03584L32')
                   if 'LTO9' in catalogue.writes(drive)]
        self.assertEqual(writers[-1], 'ULT3580-HH9')
        self.assertEqual(
            catalogue.default_drive_for('IBM', '03584L32', media='LTO9'),
            'ULT3580-TD9')

    def test_no_drive_writes_it_is_none_rather_than_a_guess(self):
        self.assertIsNone(
            catalogue.default_drive_for('SONY', 'LIB-302', media='LTO9'))


class DensityOrderTests(SimpleTestCase):
    """A long list of densities has to be readable."""

    def test_generations_are_in_generation_order(self):
        """Sorting the strings gives LTO1, LTO10, LTO10P, LTO2, which is what
        both front ends printed until media_order existed. Walking the drives
        instead gives LTO3, LTO2, LTO1, LTO4, because a drive lists its
        native density first - true, and unreadable."""
        lto = [d for d in catalogue.describe('IBM')['densities']
               if d.startswith('LTO')]
        self.assertEqual(lto[:4], ['LTO1', 'LTO2', 'LTO3', 'LTO4'])
        self.assertEqual(lto[-2:], ['LTO10', 'LTO10P'])

    def test_families_are_grouped(self):
        densities = catalogue.describe('STK')['densities']
        families = [catalogue.media_family(d) for d in densities]
        self.assertEqual(families, sorted(families),
                         'a family appears twice, so they are not grouped')

    def test_the_families_themselves(self):
        self.assertEqual(catalogue.media_family('LTO10'), 'LTO')
        self.assertEqual(catalogue.media_family('T10KB'), 'T10000')
        self.assertEqual(catalogue.media_family('E06'), '3592')
        self.assertEqual(catalogue.media_family('SDLT600'), 'SDLT')
        self.assertEqual(catalogue.media_family('AIT4'), 'AIT')

    def test_each_density_once(self):
        densities = catalogue.describe('IBM')['densities']
        self.assertEqual(len(densities), len(set(densities)))


class ProfileCommandTests(TestCase):
    """The two verbs. Printing only: every answer is one call into the core."""

    def test_list_prints_the_names_and_nothing_else(self):
        code, out, err = run(['profile', 'list'])
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), list(data.list_profiles()))

    def test_list_with_a_density_leaves_out_what_cannot_write_it(self):
        code, out, err = run(['profile', 'list', '--media', 'LTO9'])
        self.assertEqual(code, 0)
        self.assertIn('IBM', out.split())
        self.assertNotIn(NO_LTO9, out.split())

    def test_list_long_is_the_table_with_the_defaults(self):
        code, out, err = run(['profile', 'list', '--long'])
        self.assertEqual(code, 0)
        self.assertIn('DEFAULT MEDIA', out)
        self.assertIn('AIT4', out)        # SONY's, the one that catches people
        self.assertIn('T10KC', out)       # STK's

    def test_list_refuses_a_density_nobody_writes_and_says_why(self):
        code, out, err = run(['profile', 'list', '--media', 'LTO42'])
        self.assertEqual(code, 1)
        self.assertIn('LTO42', err)
        self.assertIn('only reads it', err)

    def test_show_prints_all_three_sections(self):
        code, out, err = run(['profile', 'show', 'IBM'])
        self.assertEqual(code, 0)
        self.assertIn('LIBRARY MODEL', out)
        self.assertIn('DRIVE MODEL', out)
        self.assertIn('densities', out)
        self.assertIn('03584L32', out)
        self.assertIn('ULT3580-TD8', out)

    def test_show_can_print_one_section(self):
        code, out, err = run(['profile', 'show', 'IBM', '--drives'])
        self.assertEqual(code, 0)
        self.assertIn('DRIVE MODEL', out)
        self.assertNotIn('LIBRARY MODEL', out)

    def test_show_says_what_it_narrowed_to(self):
        """A filtered list that does not say it is filtered is a lie about
        the catalogue."""
        code, out, err = run(['profile', 'show', 'IBM', '--media', 'LTO9'])
        self.assertEqual(code, 0)
        self.assertIn('write LTO9', out)

    def test_show_refuses_an_unknown_vendor_with_the_list(self):
        code, out, err = run(['profile', 'show', 'NOSUCH'])
        self.assertEqual(code, 1)
        self.assertIn('not a vendor profile', err)
        self.assertIn('IBM', err)

    def test_the_noun_alone_prints_its_help(self):
        code, out, err = run(['profile'])
        self.assertEqual(code, 2)
        self.assertIn('list', out + err)

    def test_it_has_no_write_verbs(self):
        """Nothing may edit a profile - the rule at the top of
        profiles/data.py. The noun looks like `preset`, which does have them,
        and that difference is the point."""
        import argparse

        from mhvtl_cli.main import build_parser

        parser = build_parser()
        nouns = next(action for action in parser._actions
                     if isinstance(action, argparse._SubParsersAction)).choices
        verbs = next(action for action in nouns['profile']._actions
                     if isinstance(action, argparse._SubParsersAction)).choices
        self.assertEqual(sorted(verbs), ['list', 'show'])


class RemovedVerbTests(DjangoSimpleTestCase):

    def test_drive_models_is_gone(self):
        """It is `profile show` now. `drive` is the noun for the drives on
        this host, and a catalogue is not one of them."""
        import argparse

        from mhvtl_cli.main import build_parser

        parser = build_parser()
        nouns = next(action for action in parser._actions
                     if isinstance(action, argparse._SubParsersAction)).choices
        verbs = next(action for action in nouns['drive']._actions
                     if isinstance(action, argparse._SubParsersAction)).choices
        self.assertNotIn('models', verbs)
        self.assertEqual(sorted(verbs), ['add', 'list', 'remove', 'show'])

    def test_the_service_method_behind_it_is_gone_too(self):
        """Dead code with a passing test is worse than no test: it was the
        only caller left once the verb went."""
        from apps.libraries.services.drives import DriveService

        self.assertFalse(hasattr(DriveService, 'models_for_profile'))
