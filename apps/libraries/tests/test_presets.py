"""Named library configurations: the TOML parser and renderer.

A preset is a configuration an operator composed from a profile's supported
options and named. These tests pin the file format, and in particular the
three things that would be silently wrong rather than loudly wrong:

    - an unknown key must be an error, never ignored. A preset whose
      `drive-model` quietly did nothing would send someone hunting in the
      wrong place.
    - drive order must survive, because it is slot order and therefore SCSI
      target order; a backup application that addresses drives by position
      notices when they move.
    - the keys in the file are the ones typed on the command line, so a
      preset reads as the command it replaces.

No file is touched here: parse() takes text and render() returns it, the way
device_conf.py and library_contents.py do beside it.
"""
import contextlib
import io
import re
import shlex
from pathlib import Path
from unittest import mock

from mhvtl_cli import privileges

from .base import SimpleTestCase, TestCase
from .test_cli import run

from apps.libraries.services.config import presets

PROFILES = ('ADIC', 'DELL', 'HP', 'IBM', 'OVERLAND', 'QUANTUM', 'SONY',
            'SPECTRA', 'STK')

MIXED = '''
[lib-ten]
profile = "IBM"
model = "03584L32"
empty_slots = 7

[[lib-ten.drive]]
model = "ULT3580-TD8"
count = 2

[[lib-ten.drive]]
model = "ULT3580-TD6"
count = 2

[[lib-ten.media]]
density = "LTO8"
count = 20

[[lib-ten.media]]
density = "LTO6"
count = 10
'''


class ParseTests(SimpleTestCase):

    def test_the_cli_names_become_the_specification_names(self):
        """`drives` and `tapes` are what you type; num_drives and media_count
        are what every service downstream already expects."""
        got = presets.parse('[small]\nprofile = "STK"\ndrives = 2\ntapes = 3\n',
                            profile_names=PROFILES)
        self.assertEqual(got, {'small': {'profile': 'STK',
                                         'num_drives': 2,
                                         'media_count': 3}})

    def test_mixed_drives_and_media_keep_their_order(self):
        got = presets.parse(MIXED, profile_names=PROFILES)['lib-ten']
        self.assertEqual([d['model'] for d in got['drive']],
                         ['ULT3580-TD8', 'ULT3580-TD6'])
        self.assertEqual([d['count'] for d in got['drive']], [2, 2])
        self.assertEqual([m['density'] for m in got['media']], ['LTO8', 'LTO6'])
        self.assertEqual([m['count'] for m in got['media']], [20, 10])

    def test_a_count_defaults_to_one(self):
        got = presets.parse('[one]\nprofile = "IBM"\n'
                            '[[one.drive]]\nmodel = "ULT3580-TD8"\n',
                            profile_names=PROFILES)
        self.assertEqual(got['one']['drive'], [{'model': 'ULT3580-TD8',
                                                'count': 1}])

    def test_an_empty_file_is_no_presets_not_an_error(self):
        self.assertEqual(presets.parse('', profile_names=PROFILES), {})
        self.assertEqual(presets.parse('# only a comment\n',
                                       profile_names=PROFILES), {})


class RefusalTests(SimpleTestCase):
    """Every one of these would otherwise be a silent surprise."""

    def refusal(self, text):
        with self.assertRaises(presets.PresetError) as raised:
            presets.parse(text, profile_names=PROFILES)
        return str(raised.exception)

    def test_an_unknown_key_is_named_not_ignored(self):
        message = self.refusal('[x]\nprofile = "IBM"\ndrive-model = "TD8"\n')
        self.assertIn("unknown key 'drive-model'", message)
        self.assertIn('known keys:', message)
        self.assertIn('drive_model', message)

    def test_a_profile_name_cannot_be_a_preset_name(self):
        message = self.refusal('[IBM]\nprofile = "IBM"\n')
        self.assertIn('IBM', message)
        self.assertIn('vendor profile, not a configuration', message)

    def test_the_check_is_not_case_sensitive(self):
        """`[ibm]` is the same trap spelled quietly."""
        self.assertIn('not a configuration',
                      self.refusal('[ibm]\nprofile = "IBM"\n'))

    def test_broken_toml_says_so(self):
        self.assertIn('not valid TOML', self.refusal('[x\nprofile = '))

    def test_a_drive_without_a_model_is_refused(self):
        message = self.refusal('[x]\nprofile = "IBM"\n[[x.drive]]\ncount = 2\n')
        self.assertIn('needs a model', message)

    def test_a_count_must_be_a_positive_whole_number(self):
        for bad in ('0', '-1', '"two"', '1.5'):
            message = self.refusal(f'[x]\nprofile = "IBM"\n'
                                   f'[[x.drive]]\nmodel = "TD8"\ncount = {bad}\n')
            self.assertIn('positive whole number', message)

    def test_a_drive_list_must_be_a_list_of_tables(self):
        message = self.refusal('[x]\nprofile = "IBM"\ndrive = "ULT3580-TD8"\n')
        self.assertIn('must be a list of tables', message)

    def test_an_unknown_key_inside_a_drive_is_named(self):
        message = self.refusal('[x]\nprofile = "IBM"\n'
                               '[[x.drive]]\nmodel = "TD8"\nvendor = "IBM"\n')
        self.assertIn('vendor', message)
        self.assertIn('known keys: model, count', message)


class RenderTests(SimpleTestCase):

    def test_what_is_written_can_be_read_back(self):
        """The round trip is the test that matters: --save-preset writes with
        render() and the next run reads it with parse()."""
        first = presets.parse(MIXED, profile_names=PROFILES)
        again = presets.parse(presets.render(first), profile_names=PROFILES)
        self.assertEqual(again, first)

    def test_it_writes_the_cli_names_back(self):
        text = presets.render({'small': {'profile': 'STK', 'num_drives': 2,
                                         'media_count': 3}})
        self.assertIn('drives = 2', text)
        self.assertIn('tapes = 3', text)
        self.assertNotIn('num_drives', text)
        self.assertNotIn('media_count', text)

    def test_the_header_tells_a_reader_what_to_do(self):
        """Someone who opens the file having read no documentation should still
        be able to add a preset."""
        text = presets.render({})
        for expected in ('mhvtl profile list', 'mhvtl preset list',
                         'may not take a profile', '[[lib-ten.drive]]',
                         'presets.toml.example'):
            self.assertIn(expected, text)

    def test_a_quote_in_a_value_survives_the_round_trip(self):
        odd = {'x': {'profile': 'IBM', 'library_model': 'a"b\\c'}}
        self.assertEqual(presets.parse(presets.render(odd),
                                       profile_names=PROFILES), odd)


class AsCommandsTests(SimpleTestCase):
    """What to type, composed once and printed by both front ends.

    `preset show` ends with these the way `profile show` ends with "Narrow
    it:", and the console prints the same lines under each saved
    configuration. The browser composes nothing.

    The flag spellings now live here *and* in commands/preset.py's parser,
    which is the shape of drift this project has fixed four times - so
    SuggestedCommandTests below hands every one of these to the real parser,
    and the round trip here proves the command rebuilds the preset it came
    from.
    """

    SMALL = {'profile': 'IBM', 'library_model': '03584L32',
             'drive_model': 'ULT3580-TD8', 'media_type': 'LTO8',
             'num_drives': 2, 'media_count': 20, 'empty_slots': 4}

    def commands(self, name, spec):
        return {line['label']: line['command']
                for line in presets.as_commands(name, spec)}

    def test_a_usable_preset_gets_both_lines(self):
        said = self.commands('lab-big', self.SMALL)
        self.assertEqual(said['Use it'],
                         'mhvtl library create --preset lab-big')
        self.assertEqual(
            said['Rebuild it'],
            'mhvtl preset set lab-big --profile IBM --model 03584L32 '
            '--drive-model ULT3580-TD8 --media-type LTO8 --drives 2 '
            '--tapes 20 --empty-slots 4')

    def test_no_id_because_create_allocates_one(self):
        """The shorter command is also the more correct one, and an id is a
        property of a host this module cannot see."""
        self.assertNotIn('--id', self.commands('lab-big', self.SMALL)['Use it'])

    def test_a_half_built_preset_is_not_offered_as_usable(self):
        """`--preset bigger` is refused when it names no profile, so printing
        "Use it" would hand somebody a command that cannot run."""
        said = self.commands('bigger', {'num_drives': 8})
        self.assertNotIn('Use it', said)
        self.assertEqual(said['Rebuild it'],
                         'mhvtl preset set bigger --drives 8')

    def test_a_mixed_preset_repeats_the_flag_once_per_kind(self):
        said = self.commands('lib-ten', {
            'profile': 'IBM',
            'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                      {'model': 'ULT3580-TD6', 'count': 2}],
            'media': [{'density': 'LTO8', 'count': 20}]})
        self.assertEqual(said['Rebuild it'],
                         'mhvtl preset set lib-ten --profile IBM '
                         '--drive ULT3580-TD8:2 --drive ULT3580-TD6:2 '
                         '--media LTO8:20')

    def test_a_list_never_appears_beside_the_count_it_implies(self):
        """`--drives 4 --drive X:2` is refused by the command it would be
        pasted into, so composing it would produce a command this project's
        own parser rejects."""
        said = self.commands('odd', {'profile': 'IBM', 'num_drives': 4,
                                     'drive': [{'model': 'ULT3580-TD8',
                                                'count': 2}]})
        self.assertNotIn('--drives', said['Rebuild it'])
        self.assertIn('--drive ULT3580-TD8:2', said['Rebuild it'])

    def test_a_model_with_a_space_is_quoted(self):
        """Quantum makes an 'SDLT 320' and ADIC a 'Scalar i2000'. Unquoted,
        the command parses as two arguments and does the wrong thing."""
        said = self.commands('q', {'profile': 'QUANTUM',
                                   'library_model': 'Scalar i500',
                                   'drive_model': 'SDLT 320'})
        self.assertIn("--model 'Scalar i500'", said['Rebuild it'])
        self.assertIn("--drive-model 'SDLT 320'", said['Rebuild it'])

    def test_the_rebuild_command_rebuilds_the_preset(self):
        """The round trip, which is the test that matters: parse what it
        prints with the real CLI parser and the values come back."""
        import shlex

        from mhvtl_cli.main import build_parser

        for name, spec in (('lab-big', self.SMALL),
                           ('q', {'profile': 'QUANTUM',
                                  'library_model': 'Scalar i500'}),
                           ('mixed', {'profile': 'IBM',
                                      'drive': [{'model': 'ULT3580-TD6',
                                                 'count': 3}]})):
            with self.subTest(preset=name):
                command = self.commands(name, spec)['Rebuild it']
                args = build_parser().parse_args(shlex.split(command)[1:])
                self.assertEqual(args.name, name)
                for key, value in spec.items():
                    if key in presets.FLAG_FOR:
                        self.assertEqual(getattr(args, key), value)
                for entry in spec.get('drive') or []:
                    self.assertIn(f"{entry['model']}:{entry['count']}",
                                  args.drive_runs)

    def test_every_settable_key_has_a_flag(self):
        """A key the format can hold and this cannot spell would be dropped
        silently from the command it composes."""
        self.assertEqual(set(presets.FLAG_FOR), set(presets.KEY_TO_SPEC.values()))
        self.assertEqual(set(presets.FLAG_FOR_LIST), set(presets.LIST_KEYS))


class PresetOperationTests(SimpleTestCase):
    """The operations on the file: build one up, use it, remove it.

    Every test writes into a temporary directory, so the host's own
    /etc/mhvtl-gui/presets.toml is never touched - the same discipline the
    config tests use for /etc/mhvtl.
    """

    #: A complete preset, for the tests that care about what a preset holds
    #: rather than about building one.
    SMALL = {'profile': 'IBM', 'library_model': '03584L32',
             'drive_model': 'ULT3580-TD8', 'media_type': 'LTO8',
             'num_drives': 2, 'media_count': 20, 'empty_slots': 4}

    def setUp(self):
        from apps.libraries.services.libraries import presets as operations
        self.operations = operations
        self.base = self.tmpdir()

    def test_a_missing_file_is_no_presets_not_an_error(self):
        """A host that has never saved one is not misconfigured."""
        listed = self.operations.names(base=self.base)
        self.assertTrue(listed.success)
        self.assertEqual(listed.data['presets'], [])

    # -- rename ------------------------------------------------------------
    #
    # Nothing could do this before 5 October 2026: not the command line, not
    # the console, not a service. Renaming meant editing presets.toml by
    # hand, and that file is root-owned.

    def test_rename_moves_everything_the_preset_holds(self):
        self.operations.save('lab-small', dict(self.SMALL), base=self.base)
        moved = self.operations.rename('lab-small', 'lab-big', base=self.base)

        self.assertTrue(moved.success, moved.errors)
        self.assertEqual(moved.data['spec'], self.SMALL)
        self.assertEqual([row['name'] for row
                          in self.operations.names(base=self.base).data['presets']],
                         ['lab-big'])

    def test_rename_keeps_a_half_built_preset_half_built(self):
        """Nothing is revalidated: the configuration did not change, only its
        key, and a preset that was valid under one name cannot become invalid
        under another."""
        self.operations.save('bits', {'num_drives': 8}, base=self.base)
        moved = self.operations.rename('bits', 'more-bits', base=self.base)

        self.assertTrue(moved.success, moved.errors)
        self.assertEqual(moved.data['spec'], {'num_drives': 8})

    def test_rename_refuses_a_name_that_is_taken(self):
        """Renaming onto one would throw away what it holds, silently."""
        self.operations.save('one', {'profile': 'IBM'}, base=self.base)
        self.operations.save('two', {'profile': 'STK'}, base=self.base)

        refused = self.operations.rename('one', 'two', base=self.base)
        self.assertFalse(refused.success)
        self.assertIn('already exists', refused.message)
        self.assertEqual(
            self.operations.describe('two', base=self.base).data['spec'],
            {'profile': 'STK'}, 'the preset it refused to overwrite')

    def test_rename_refuses_a_vendors_name(self):
        self.operations.save('mine', {'profile': 'IBM'}, base=self.base)
        refused = self.operations.rename('mine', 'IBM', base=self.base)
        self.assertFalse(refused.success)
        self.assertIn('vendor profile', refused.message)

    def test_rename_refuses_a_name_the_file_cannot_hold(self):
        """`[lab.small]` is a NESTED table in TOML, so the file would come
        back with a key called 'small' inside one called 'lab' - and refuse
        to load at all, taking every other preset with it."""
        self.operations.save('mine', {'profile': 'IBM'}, base=self.base)
        for bad in ('lab.small', 'lab small', 'lab]small', '', 'lab"small'):
            with self.subTest(name=bad):
                refused = self.operations.rename('mine', bad, base=self.base)
                self.assertFalse(refused.success, f'{bad!r} was accepted')
        # and the file still loads
        self.assertTrue(self.operations.names(base=self.base).success)

    def test_save_refuses_the_same_names(self):
        """The guard belongs to both writers: a check in one of them only is
        a file the other can corrupt."""
        refused = self.operations.save('lab.small', {'profile': 'IBM'},
                                       base=self.base)
        self.assertFalse(refused.success)
        self.assertIn('cannot be a preset name', refused.message)

    def test_rename_refuses_a_preset_that_is_not_there(self):
        refused = self.operations.rename('ghost', 'other', base=self.base)
        self.assertFalse(refused.success)
        self.assertIn("no preset called 'ghost'", refused.message)

    def test_rename_to_its_own_name_changes_nothing(self):
        self.operations.save('mine', {'profile': 'IBM'}, base=self.base)
        refused = self.operations.rename('mine', 'mine', base=self.base)
        self.assertFalse(refused.success)
        self.assertIn('already called that', refused.message)
        self.assertTrue(self.operations.describe('mine', base=self.base).success)

    def test_the_renamed_preset_is_what_create_resolves(self):
        """The point of the whole thing: the new name is usable at once."""
        self.operations.save('lab-small', dict(self.SMALL), base=self.base)
        self.operations.rename('lab-small', 'lab-big', base=self.base)

        self.assertFalse(self.operations.resolve('lab-small',
                                                 base=self.base).success)
        resolved = self.operations.resolve('lab-big', base=self.base)
        self.assertTrue(resolved.success, resolved.errors)
        self.assertEqual(resolved.data['spec'], self.SMALL)

    def test_it_is_built_up_a_piece_at_a_time_in_any_order(self):
        """The order an operator actually uses: a count before the vendor."""
        first = self.operations.save('tape-ten', {'num_drives': 2},
                                     base=self.base)
        self.assertTrue(first.success, first.errors)
        self.assertFalse(first.data['complete'],
                         'no profile yet, so not usable')

        second = self.operations.save('tape-ten', {'profile': 'IBM'},
                                      base=self.base)
        self.assertTrue(second.success, second.errors)
        self.assertTrue(second.data['complete'])
        self.assertEqual(second.data['spec']['num_drives'], 2,
                         'the earlier piece survived')

    def test_drives_append_and_keep_their_order(self):
        self.operations.save('m', {'profile': 'IBM'}, base=self.base)
        self.operations.save('m', {'drive': [{'model': 'ULT3580-TD8',
                                              'count': 2}]}, base=self.base)
        saved = self.operations.save('m', {'drive': [{'model': 'ULT3580-TD6',
                                                      'count': 2}]},
                                     base=self.base)
        self.assertEqual([d['model'] for d in saved.data['spec']['drive']],
                         ['ULT3580-TD8', 'ULT3580-TD6'])

    def test_a_named_list_replaces_instead_of_appending(self):
        """`--drive` overwrites what `--add-drive` accumulated."""
        self.operations.save('m', {'profile': 'IBM',
                                   'drive': [{'model': 'ULT3580-TD8',
                                              'count': 2}]}, base=self.base)
        saved = self.operations.save('m', {'drive': [{'model': 'ULT3580-TD9',
                                                      'count': 4}]},
                                     replace_lists=('drive',), base=self.base)
        self.assertEqual(saved.data['spec']['drive'],
                         [{'model': 'ULT3580-TD9', 'count': 4}])

    def test_the_whole_preset_is_checked_not_only_what_arrived(self):
        """A drive that is a real IBM model but not one this library takes."""
        self.operations.save('m', {'profile': 'IBM',
                                   'library_model': '03584L32'},
                             base=self.base)
        refused = self.operations.save('m', {'drive_model': 'T10000C'},
                                       base=self.base)
        self.assertFalse(refused.success)
        self.assertIn('not compatible with library model', refused.message)
        self.assertTrue(any('supports drives' in f for f in refused.errors),
                        'the refusal has to say what it does take')

    def test_a_refused_change_is_not_written(self):
        self.operations.save('m', {'profile': 'IBM',
                                   'library_model': '03584L32'},
                             base=self.base)
        self.operations.save('m', {'drive_model': 'T10000C'}, base=self.base)
        again = self.operations.resolve('m', base=self.base)
        self.assertTrue(again.success)
        self.assertNotIn('drive_model', again.data['spec'])

    def test_a_preset_cannot_take_a_profiles_name(self):
        refused = self.operations.save('IBM', {'profile': 'IBM'},
                                       base=self.base)
        self.assertFalse(refused.success)
        self.assertIn('vendor profile, not a configuration', refused.message)

    def test_resolving_an_unknown_name_lists_what_exists(self):
        """A mistyped name is when somebody most wants the list."""
        self.operations.save('lab-small', {'profile': 'STK'}, base=self.base)
        missing = self.operations.resolve('lab-smal', base=self.base)
        self.assertFalse(missing.success)
        self.assertIn("no preset called 'lab-smal'", missing.message)
        self.assertTrue(any('lab-small' in e for e in missing.errors))

    def test_an_incomplete_preset_cannot_be_used_and_says_why(self):
        self.operations.save('half', {'num_drives': 2}, base=self.base)
        unusable = self.operations.resolve('half', base=self.base)
        self.assertFalse(unusable.success)
        self.assertIn('names no profile', unusable.message)

    def test_a_resolved_preset_is_what_apply_defaults_completes(self):
        """The whole point: a preset is a partial specification, so the
        existing creation path needs no new merge logic."""
        from apps.libraries.services.libraries import spec

        self.operations.save('lib-ten', {'profile': 'IBM', 'num_drives': 2},
                             base=self.base)
        resolved = self.operations.resolve('lib-ten', base=self.base)
        filled = spec.apply_defaults({**resolved.data['spec'],
                                      'library_id': 90})
        self.assertEqual(filled['num_drives'], 2, 'the preset won')
        self.assertEqual(filled['product'], '03584L32', 'the profile filled in')
        self.assertEqual(filled['drive_product'], 'ULT3580-TD8')

    def test_forget_removes_one_key(self):
        self.operations.save('m', {'profile': 'IBM', 'num_drives': 2},
                             base=self.base)
        self.assertTrue(self.operations.forget('m', ['num_drives'],
                                               base=self.base).success)
        left = self.operations.resolve('m', base=self.base)
        self.assertNotIn('num_drives', left.data['spec'])

    def test_forget_with_no_keys_deletes_the_preset(self):
        self.operations.save('m', {'profile': 'IBM'}, base=self.base)
        self.assertTrue(self.operations.forget('m', [], base=self.base).success)
        self.assertEqual(self.operations.names(base=self.base).data['presets'], [])

    def test_forgetting_a_key_it_does_not_have_says_what_it_has(self):
        self.operations.save('m', {'profile': 'IBM'}, base=self.base)
        refused = self.operations.forget('m', ['tapes'], base=self.base)
        self.assertFalse(refused.success)
        self.assertTrue(any('it has:' in e for e in refused.errors))

    def test_what_is_saved_survives_a_round_trip_through_the_file(self):
        """Checked through load() rather than resolve(), because resolve()
        refuses a preset with drive or media lists until creation can honour
        one - see NotYetSupportedTests. The storage carries them correctly in
        the meantime, which is what this pins."""
        self.operations.save('lib-ten', {
            'profile': 'IBM', 'library_model': '03584L32', 'empty_slots': 7,
            'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                      {'model': 'ULT3580-TD6', 'count': 2}],
            'media': [{'density': 'LTO8', 'count': 20},
                      {'density': 'LTO6', 'count': 10}]}, base=self.base)
        spec = self.operations.load(base=self.base)['lib-ten']
        self.assertEqual([d['count'] for d in spec['drive']], [2, 2])
        self.assertEqual([m['density'] for m in spec['media']],
                         ['LTO8', 'LTO6'])
        self.assertEqual(spec['empty_slots'], 7)

    def test_the_file_is_written_where_core_paths_says(self):
        """Rule 5: the path is a setting, not a literal in this module."""
        from apps.libraries.services.core.paths import presets_path

        self.operations.save('m', {'profile': 'IBM'}, base=self.base)
        self.assertTrue(presets_path(self.base).exists())


class MixedPresetTests(SimpleTestCase):
    """A preset that asks for mixed drives and media creates one.

    These tests were the opposite of themselves until 4 October 2026: the file
    format parsed [[name.drive]] arrays, device_conf wrote one drive model for
    every slot, and so a preset carrying a list had to be *refused* rather
    than read, accepted and quietly ignored. apply_defaults turns either shape
    into drive_slots and media_runs now, which is what made the refusal
    unnecessary - and these the tests of a feature instead of a limit.
    """

    def setUp(self):
        from apps.libraries.services.libraries import presets as operations
        from apps.libraries.services.libraries import spec
        self.operations = operations
        self.spec = spec
        self.base = self.tmpdir()

    def _filled(self, name):
        resolved = self.operations.resolve(name, base=self.base)
        self.assertTrue(resolved.success, resolved.message)
        return self.spec.apply_defaults({**resolved.data['spec'],
                                         'library_id': 10})

    def test_a_mixed_drive_preset_resolves_and_fills_one_slot_each(self):
        self.operations.save('lib-ten', {
            'profile': 'IBM',
            'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                      {'model': 'ULT3580-TD6', 'count': 2}]}, base=self.base)

        filled = self._filled('lib-ten')
        self.assertEqual([slot['product'] for slot in filled['drive_slots']],
                         ['ULT3580-TD8', 'ULT3580-TD8',
                          'ULT3580-TD6', 'ULT3580-TD6'],
                         'the order of the tables is slot order')
        self.assertEqual(filled['num_drives'], 4,
                         'the list says how many, so nothing else has to')

    def test_a_mixed_media_preset_keeps_its_runs_in_order(self):
        self.operations.save('two-kinds', {
            'profile': 'IBM', 'library_model': '03584L32',
            'drive': [{'model': 'ULT3580-TD8', 'count': 1},
                      {'model': 'ULT3580-TD6', 'count': 1}],
            'media': [{'density': 'LTO8', 'count': 3},
                      {'density': 'LTO6', 'count': 2}]}, base=self.base)

        filled = self._filled('two-kinds')
        self.assertEqual([(run['density'], run['count'])
                          for run in filled['media_runs']],
                         [('LTO8', 3), ('LTO6', 2)])
        self.assertEqual(filled['media_count'], 5)
        self.assertEqual(filled['media_type'], 'LTO8',
                         'the nominal density is the first one a drive writes')

    def test_a_single_entry_list_is_the_list_form_too(self):
        """One entry is a list, and goes the same way as two."""
        self.operations.save('one', {'profile': 'IBM',
                                     'media': [{'density': 'LTO8',
                                                'count': 5}]},
                             base=self.base)
        filled = self._filled('one')
        self.assertEqual(filled['media_count'], 5)
        self.assertEqual([run['density'] for run in filled['media_runs']],
                         ['LTO8'])

    def test_a_scalar_preset_is_unaffected(self):
        self.operations.save('plain', {'profile': 'IBM', 'num_drives': 2,
                                       'media_type': 'LTO8'}, base=self.base)
        filled = self._filled('plain')
        self.assertEqual(len(filled['drive_slots']), 2)
        self.assertEqual([run['density'] for run in filled['media_runs']],
                         ['LTO8'])

    def test_a_list_settles_what_it_implies_rather_than_leaving_it_open(self):
        """`preset show` listed "4 drives, 30 tapes" under "left to IBM" for a
        preset whose own lists asked for four drives and thirty cartridges.
        The list is the answer, so neither half may report it as still open -
        and the counts are not stored beside it either."""
        self.operations.save('lib-ten', {
            'profile': 'IBM', 'library_model': '03584L32',
            'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                      {'model': 'ULT3580-TD6', 'count': 2}],
            'media': [{'density': 'LTO8', 'count': 20},
                      {'density': 'LTO6', 'count': 10}]}, base=self.base)

        described = self.operations.describe('lib-ten', base=self.base).data
        for implied in ('num_drives', 'drive_model',
                        'media_count', 'media_type'):
            self.assertNotIn(implied, described['from_profile'],
                             f'{implied} is settled by a list, not by IBM')
            self.assertNotIn(implied, described['fixed'],
                             f'{implied} would be the list said twice')
        self.assertEqual(len(described['fixed']['drive']), 2)
        self.assertEqual(len(described['fixed']['media']), 2)
        self.assertTrue(described['complete'])

    def test_the_breakdown_comes_with_the_total_it_adds_up_to(self):
        """"20 x LTO8 + 10 x LTO6" never said thirty, and the number of
        cartridges is the thing an operator is deciding about. One sentence,
        two callers: `preset show` prints it under the breakdown and the
        vendor page's card shows the same words."""
        self.operations.save('lib-ten', {
            'profile': 'IBM', 'library_model': '03584L32',
            'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                      {'model': 'ULT3580-TD6', 'count': 2}],
            'media': [{'density': 'LTO8', 'count': 20},
                      {'density': 'LTO6', 'count': 10}]}, base=self.base)

        self.assertEqual(
            self.operations.describe('lib-ten', base=self.base).data['holds'],
            '4 drive(s), 30 cartridge(s)')
        listed = [row for row in self.operations.names(base=self.base)
                  .data['presets'] if row['name'] == 'lib-ten']
        self.assertEqual(listed[0]['holds'], '4 drive(s), 30 cartridge(s)')

    def test_one_line_says_the_vendor_the_model_and_the_totals(self):
        """The headline for a caller with one line to give it: `preset list`'s
        column and the folded card on the vendor page, from one sentence.
        Never the breakdown - that is what the line opens onto."""
        self.operations.save('lib-ten', {
            'profile': 'IBM', 'library_model': '03584L32',
            'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                      {'model': 'ULT3580-TD6', 'count': 2}],
            'media': [{'density': 'LTO8', 'count': 20},
                      {'density': 'LTO6', 'count': 10}]}, base=self.base)
        self.operations.save('plain', {
            'profile': 'IBM', 'library_model': '3573-TL',
            'num_drives': 3, 'media_count': 7}, base=self.base)

        brief = {row['name']: row['brief'] for row
                 in self.operations.names(base=self.base).data['presets']}
        self.assertEqual(brief['lib-ten'],
                         'IBM, 03584L32, 4 drive(s), 30 cartridge(s)')
        self.assertEqual(brief['plain'],
                         'IBM, 3573-TL, 3 drive(s), 7 cartridge(s)')
        self.assertNotIn('ULT3580', brief['lib-ten'])

    def test_one_line_for_a_preset_that_fixes_almost_nothing(self):
        """A preset holding nothing but a vendor is a whole configuration as
        a concept, and "IBM" is the honest one-line version of it."""
        self.operations.save('bare', {'profile': 'IBM'}, base=self.base)
        self.operations.save('nameless', {'num_drives': 8}, base=self.base)
        brief = {row['name']: row['brief'] for row
                 in self.operations.names(base=self.base).data['presets']}
        self.assertEqual(brief['bare'], 'IBM')
        self.assertEqual(brief['nameless'], '8 drive(s)')

    def test_a_single_kind_preset_says_its_counts_once(self):
        """It already reads "2 drives, 20 tapes" in the breakdown, so a
        second sentence saying two and twenty would be noise."""
        self.operations.save('plain', {
            'profile': 'IBM', 'num_drives': 2, 'media_count': 20},
            base=self.base)
        self.assertEqual(
            self.operations.describe('plain', base=self.base).data['holds'], '')

    def test_a_count_beside_a_list_is_refused_even_when_hand_merged(self):
        """The file and the command line refuse the pair, and this is the net
        under them: `--add-drive` and then `--drives 8` arrives here without
        passing either edge."""
        refused = self.operations.save('contradictory', {
            'profile': 'IBM',
            'drive': [{'model': 'ULT3580-TD8', 'count': 2}],
            'num_drives': 8}, base=self.base)
        self.assertFalse(refused.success, refused.message)
        self.assertIn('8 drives were asked for but the list adds up to 2',
                      refused.message)


class SavePresetTests(SimpleTestCase):
    """`--save-preset`: keeping a configuration that worked.

    What goes in the preset is what the operator *asked for*, not the filled-in
    specification. That matters: a preset recording `profile = "STK"` and
    nothing else follows the profile's defaults as they change, while one
    recording every derived value would freeze today's defaults for ever.
    """

    def setUp(self):
        from mhvtl_cli.commands import library as command
        from apps.libraries.services.libraries import presets as operations
        self.command = command
        self.operations = operations
        self.base = self.tmpdir()

    def test_it_keeps_what_was_asked_for_and_drops_the_id(self):
        """library_id is the one thing that must differ next time."""
        spec = {'profile': 'STK', 'library_id': 80, 'num_drives': 2,
                'media_count': 3, 'drive_model': 'ULT3580-TD8',
                'media_type': 'LTO8'}
        kept = self.command._savable(spec)
        self.assertNotIn('library_id', kept)
        self.assertEqual(kept['profile'], 'STK')
        self.assertEqual(kept['num_drives'], 2)
        self.assertEqual(kept['drive_model'], 'ULT3580-TD8')

    def test_it_drops_the_aliases_apply_defaults_derives(self):
        """product and drive_product are set from library_model and
        drive_model, so storing both would be the same fact twice under two
        names - and they could then disagree."""
        kept = self.command._savable({'profile': 'IBM',
                                      'library_model': '03584L32',
                                      'product': '03584L32',
                                      'drive_model': 'ULT3580-TD8',
                                      'drive_product': 'ULT3580-TD8'})
        self.assertEqual(sorted(kept), ['drive_model', 'library_model',
                                        'profile'])

    def test_a_saved_preset_recreates_the_same_specification(self):
        """The round trip that matters: save what was used, resolve it, and
        the completed specification matches."""
        from apps.libraries.services.libraries import spec as spec_module

        asked = {'profile': 'STK', 'num_drives': 2, 'media_count': 3,
                 'empty_slots': 3, 'drive_model': 'ULT3580-TD8',
                 'media_type': 'LTO8'}
        saved = self.operations.save('lab-small',
                                     self.command._savable(asked),
                                     base=self.base)
        self.assertTrue(saved.success, saved.errors)

        resolved = self.operations.resolve('lab-small', base=self.base)
        self.assertTrue(resolved.success, resolved.errors)

        first = spec_module.apply_defaults({**asked, 'library_id': 80})
        again = spec_module.apply_defaults({**resolved.data['spec'],
                                            'library_id': 90})
        for field in ('vendor', 'product', 'drive_vendor', 'drive_product',
                      'media_type', 'num_drives', 'media_count',
                      'empty_slots'):
            self.assertEqual(again[field], first[field],
                             f'{field} differs between the original and the '
                             f'preset')

    def test_the_settable_keys_are_exactly_the_preset_format(self):
        """Equal, not merely contained. A key `preset set` accepts that the
        file cannot express is written and lost on the next read; a key the
        file accepts with no option for it can only be written by hand -
        `size_mb` was the second kind, and mapped to a specification key
        nothing reads at all."""
        from apps.libraries.services.config.presets import KEY_TO_SPEC
        from mhvtl_cli.commands import preset as preset_command

        self.assertEqual(tuple(KEY_TO_SPEC.values()),
                         preset_command._SETTABLE)

    def test_every_preset_set_option_is_one_a_preset_can_hold(self):
        """`preset set --serial FOO` parsed and then said "nothing to set":
        do_set only reads the keys in _SETTABLE, so an option outside it is
        accepted and ignored. Walking the real parser is the only way that
        shows up."""
        import argparse

        from mhvtl_cli.commands import preset as preset_command
        from mhvtl_cli.main import build_parser

        parser = build_parser()
        for name in ('preset', 'set'):
            subparsers = next(action for action in parser._actions
                              if isinstance(action, argparse._SubParsersAction))
            parser = subparsers.choices[name]

        from mhvtl_cli import runs

        # The scalars, and the list flags runs.py registers. A list is not in
        # _SETTABLE because it is not a scalar the format maps - it is the
        # `[[name.drive]]` array - but do_set reads it, so it is accounted
        # for here rather than looking like an option nothing reads.
        settable = set(preset_command._SETTABLE)
        for kind in runs.KINDS:
            settable |= {kind.dest, f'add_{kind.dest}'}

        for action in parser._actions:
            if not action.option_strings or action.dest == 'help':
                continue
            self.assertIn(action.dest, settable,
                          f'{action.option_strings} is accepted by '
                          f'`preset set` and then ignored')


# -- the shipped example ----------------------------------------------------
#
# packaging/presets.toml.example is installed at /etc/mhvtl-gui and read by
# nobody: the commands write presets.toml, and an upgrade replaces the
# example. Nothing breaks when it is wrong, which is exactly why it is tested.
# An example naming a drive its library model cannot take, or a command that
# does not parse, teaches the wrong thing to the one person who opened the
# file instead of the documentation.

GUI = Path(__file__).resolve().parents[3]
EXAMPLE_FILE = GUI / 'packaging' / 'presets.toml.example'


def _commands_in(text):
    """Every `mhvtl ...` command a comment offers, ready to hand to argparse.

    Lines are joined across a trailing backslash, so the three-line
    `library create` in the example is checked as the one command it is. The
    header's two-column table puts a description after the command, separated
    by two or more spaces, so the first column is taken.
    """
    found, pending = [], None
    for line in text.splitlines():
        if '#' not in line:
            pending = None
            continue
        after = line.split('#', 1)[1].strip()
        if pending is not None:
            pending = f'{pending} {after}'
        elif after.startswith('mhvtl '):
            pending = after
        else:
            continue
        if pending.endswith('\\'):
            pending = pending[:-1].strip()
            continue
        command = re.split(r'\s{2,}', pending)[0].strip()
        pending = None
        # `preset set NAME ...` stands for "and some options"; the ellipsis is
        # prose, not an argument.
        if command.endswith('...'):
            command = command[:-3].strip()
        found.append(command)
    return found


class ShippedExampleTests(SimpleTestCase):
    """The example file is a real preset file and has to behave like one."""

    def setUp(self):
        self.text = EXAMPLE_FILE.read_text()
        self.parsed = presets.parse(self.text, profile_names=PROFILES)

    def test_it_parses_with_the_parser_that_will_read_it(self):
        """Shipped broken, it would fail on a host rather than here."""
        self.assertTrue(self.parsed, 'the example defines no presets')

    def test_every_preset_in_it_is_valid_against_its_profile(self):
        """A model the vendor does not make would be copied by whoever read
        it, and refused when they tried to use it."""
        from apps.libraries.services.libraries import validation

        for name, spec in self.parsed.items():
            checked = validation.check_partial(spec)
            self.assertTrue(checked.is_valid,
                            f'preset {name} in the example: {checked.errors}')

    def test_the_complete_ones_can_actually_create_a_library(self):
        """Used as presets.toml, each preset that names a profile resolves."""
        from apps.libraries.services.libraries import presets as operations

        base = self.tmpdir()
        (base / 'presets.toml').write_text(self.text)
        for name, spec in self.parsed.items():
            resolved = operations.resolve(name, base=base)
            if spec.get('profile'):
                self.assertTrue(resolved.success,
                                f'{name}: {resolved.message} {resolved.errors}')
            else:
                # Half-built on purpose, to show that it is allowed.
                self.assertFalse(resolved.success)
                self.assertIn('incomplete', resolved.message)

    def test_it_documents_every_key_the_format_accepts(self):
        """The header promises "every key, with a worked example of each", so
        a key added to the parser has to be added here too."""
        for key in list(presets.KEY_TO_SPEC) + list(presets.LIST_KEYS):
            self.assertIn(key, self.text,
                          f"the example does not mention '{key}'")

    def test_it_carries_the_header_a_written_file_gets(self):
        """Someone comparing their presets.toml with the example should not
        have to wonder which preamble is the current one."""
        self.assertIn(presets._HEADER, self.text,
                      'the header in config/presets.py changed: copy it to '
                      'the top of packaging/presets.toml.example, which is '
                      'where a reader compares the two')

    def test_it_ships_a_live_mixed_preset(self):
        """The arrays were commented out while creation could not honour them,
        and `create --preset` refused a preset carrying one. Creation honours
        them since 4 October 2026, so the example has to show a real one -
        syntax in a comment is how the format and the example drift apart."""
        mixed = [spec for spec in self.parsed.values()
                 if spec.get('drive') and spec.get('media')]
        self.assertTrue(mixed, 'no preset in the example mixes drives and '
                               'media, so nothing proves the arrays work')
        for spec in mixed:
            self.assertGreater(len(spec['drive']), 1,
                               'one kind of drive is not a mixed library')
            # A list says how many, so the count keys would be a second
            # answer - and the parser refuses the pair.
            self.assertNotIn('num_drives', spec)
            self.assertNotIn('media_count', spec)

    def test_the_header_names_the_example_by_the_name_it_is_installed_under(self):
        from apps.libraries.services.core import paths

        self.assertIn(paths.presets_example_path().name, presets._HEADER)


class SuggestedCommandTests(SimpleTestCase):
    """Every command a refusal or the example offers has to parse.

    Three did not. The preset header named a `library models` verb that was
    never built; `--profile`'s own help text named it too, for a week. And
    the mixed-drive refusal ended with `preset unset NAME --drive`, which
    argparse rejected with "unrecognized arguments": `unset` takes its keys
    as positional arguments. Each was handing somebody a command that could
    not run, in the message meant to get them out of trouble.

    That mixed-drive refusal is gone - creation honours the arrays now - so
    what is left to check is the refusals that remain.
    """

    def setUp(self):
        from mhvtl_cli.main import build_parser
        from apps.libraries.services.libraries import presets as operations
        self.parser = build_parser()
        self.operations = operations
        self.base = self.tmpdir()

    def _assert_parses(self, command, where):
        # shlex, not split(): a model can have a space in it - Quantum makes
        # an 'SDLT 320' - so a composed command quotes, and splitting on
        # whitespace would hand argparse two arguments where there is one.
        argv = shlex.split(command)
        self.assertEqual(argv[0], 'mhvtl', command)
        with contextlib.redirect_stderr(io.StringIO()) as complaint:
            try:
                self.parser.parse_args(argv[1:])
            except SystemExit:
                self.fail(f'{where} offers a command that does not parse:\n'
                          f'  {command}\n  {complaint.getvalue().strip()}')

    def test_the_commands_in_the_example_parse(self):
        commands = _commands_in(EXAMPLE_FILE.read_text())
        self.assertGreater(len(commands), 8, 'none were found to check')
        for command in commands:
            self._assert_parses(command, 'the example file')

    def test_the_commands_a_preset_shows_parse(self):
        """Both lines of every preset in the shipped example, against the
        real parser - which is the guard on the flag spellings now living in
        config/presets.FLAG_FOR as well as in commands/preset.py."""
        checked = 0
        for name, spec in presets.parse(EXAMPLE_FILE.read_text(),
                                        profile_names=PROFILES).items():
            for line in presets.as_commands(name, spec):
                with self.subTest(preset=name, label=line['label']):
                    self._assert_parses(line['command'],
                                        f'`preset show {name}`')
                checked += 1
        self.assertGreater(checked, 5, 'no commands were composed to check')

    def test_the_commands_in_the_written_header_parse(self):
        commands = _commands_in(presets._HEADER)
        self.assertGreater(len(commands), 3, 'none were found to check')
        for command in commands:
            self._assert_parses(command, 'the header of every preset file')

    def test_the_commands_a_refusal_suggests_parse(self):
        self.operations.save('half', {'num_drives': 2}, base=self.base)

        refusals = [self.operations.resolve('half', base=self.base),
                    self.operations.resolve('nope', base=self.base),
                    self.operations.save('IBM', {'num_drives': 1},
                                         base=self.base)]
        checked = 0
        for refused in refusals:
            self.assertFalse(refused.success, refused.message)
            for detail in refused.errors:
                if detail.startswith('mhvtl '):
                    self._assert_parses(detail, 'a preset refusal')
                    checked += 1
        self.assertEqual(checked, 1, 'the refusals that suggest a command '
                                     'changed; check the new ones parse')

    def test_a_drive_in_a_list_is_checked_when_the_preset_is_saved(self):
        """The list is where a mixed preset says which drives it wants, so it
        is what the library model has to be checked against. The checks read
        `drive_model` alone, which a mixed preset never sets: the preset was
        saved and the refusal arrived at `library create`."""
        refused = self.operations.save('bad-mix', {
            'profile': 'IBM', 'library_model': '3573-TL',
            'drive': [{'model': 'ULT3580-TD8', 'count': 1},
                      {'model': '03592J1A', 'count': 1}]}, base=self.base)
        self.assertFalse(refused.success, refused.message)
        self.assertIn("'03592J1A' is not compatible with library model "
                      "'3573-TL'", refused.message)


class ShippedExamplePackagingTests(SimpleTestCase):
    """It has to reach /etc/mhvtl-gui, and must never land as presets.toml.

    Both packagings install it. The RPM takes it from the source tarball that
    build.sh assembles, so a file the spec installs and build.sh does not copy
    fails at rpmbuild time - which is how env.template's named-file list
    nearly caught this one out.
    """

    def setUp(self):
        self.spec = (GUI / 'packaging' / 'rpm' / 'mhvtl-gui.spec').read_text()
        self.files = self.spec.split('%files', 1)[1]

    def test_the_rpm_installs_it(self):
        self.assertIn('install -m 644 packaging/presets.toml.example',
                      self.spec)
        self.assertIn('/etc/mhvtl-gui/presets.toml.example', self.files)

    def test_the_rpm_owns_the_directory_it_goes_in(self):
        self.assertIn('%dir /etc/mhvtl-gui', self.files)

    def test_nothing_packages_the_live_file(self):
        """presets.toml is the operator's. Shipping it would hand every host
        the same presets and, worse, replace theirs on an upgrade."""
        for line in self.spec.splitlines():
            if 'presets.toml' in line and not line.lstrip().startswith('#'):
                self.assertIn('presets.toml.example', line,
                              f'this line touches the live file: {line}')

    def test_the_example_is_not_marked_config(self):
        """It is documentation: an upgrade should replace it. %config would
        leave an old example behind as presets.toml.example.rpmnew."""
        for line in self.files.splitlines():
            if line.strip().endswith('presets.toml.example'):
                self.assertNotIn('%config', line)

    def test_the_source_tarball_carries_it(self):
        build = (GUI / 'packaging' / 'build.sh').read_text()
        self.assertIn('presets.toml.example', build)
        self.assertIn('"$TARBALL_DIR/packaging/"', build,
                      'the spec installs it from packaging/, so it has to '
                      'land there in the tarball')

    def test_the_tarball_installer_installs_it(self):
        installer = (GUI / 'packaging' / 'tarball' / 'install.sh').read_text()
        self.assertIn('packaging/presets.toml.example', installer)
        self.assertIn('$CONFIG_DIR/presets.toml.example', installer)


class PresetCommandTests(TestCase):
    """The `mhvtl preset` verbs.

    Its own noun since 4 October 2026: it was `mhvtl library preset ...` while
    the catalogue was `mhvtl profile ...`, which put the two halves of one
    vocabulary at two different levels. A preset is a profile you built
    yourself, so they are siblings.

    MHVTL_GUI_CONFIG_DIR points at a temporary directory, so the host's own
    /etc/mhvtl-gui/presets.toml is never read or written.
    """

    IBM_SMALL = {'profile': 'IBM', 'library_model': '03584L32',
                 'drive_model': 'ULT3580-TD8', 'media_type': 'LTO8',
                 'num_drives': 2, 'media_count': 20, 'empty_slots': 4}

    def setUp(self):
        from django.test import override_settings

        from apps.libraries.services.libraries import presets as operations

        self.operations = operations
        self.base = self.tmpdir()
        override = override_settings(MHVTL_GUI_CONFIG_DIR=str(self.base))
        override.enable()
        self.addCleanup(override.disable)

        # The write verbs are gated on membership of the mhvtl group, which is
        # a fact about the machine and not about the verb. Granted for the
        # whole class, the way test_cli.py grants it for every other mutating
        # command, because the alternative is eight tests that pass only where
        # the developer happens to be in that group - which is how they passed
        # here and failed on a GitHub runner, whose user is in adm, docker,
        # runner, systemd-journal and users.
        #
        # Nothing is skipped to achieve it. The gate itself is tested in
        # test_cli.py: can_write for root, for a group member and for neither,
        # and the refusal naming the group and the usermod command.
        granted = mock.patch.object(privileges, 'can_write', return_value=True)
        granted.start()
        self.addCleanup(granted.stop)

    def save(self, name, values):
        saved = self.operations.save(name, values)
        self.assertTrue(saved.success, saved.errors)

    # -- list -------------------------------------------------------------

    def test_list_prints_each_name_with_what_it_builds(self):
        """The names alone meant running `preset show` once per preset to
        find out which was which. The sentence beside each is the service's -
        the vendor page's card shows the same words folded."""
        self.save('ibm-small', self.IBM_SMALL)
        self.save('ibm-default', {'profile': 'IBM'})
        code, out, err = run(['preset', 'list'])
        self.assertEqual(code, 0)
        rows = {line.split()[0]: line for line in out.splitlines()
                if line[:1].isalpha() and not line.startswith('NAME')}
        self.assertEqual(sorted(rows)[:2], ['ibm-default', 'ibm-small'])
        self.assertIn('IBM, 03584L32, 2 drive(s), 20 cartridge(s)',
                      rows['ibm-small'])
        # One that fixes nothing but its vendor still says so.
        self.assertIn('IBM', rows['ibm-default'])

    def test_list_names_one_to_show(self):
        """The shape `profile list` ends with, which is where it comes from."""
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'list'])
        self.assertIn('mhvtl preset show ibm-small', out)

    def test_list_quietly_is_the_names_alone(self):
        """For a script. The columns are for a reader."""
        self.save('ibm-small', self.IBM_SMALL)
        self.save('ibm-default', {'profile': 'IBM'})
        code, out, err = run(['--quiet', 'preset', 'list'])
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ['ibm-default', 'ibm-small'])

    def test_list_says_which_cannot_be_used_yet(self):
        """A half-built preset is legal, and `--preset` will refuse it. The
        name on its own would be a trap - so the reason stands where the
        other presets say what they build."""
        self.save('ibm-small', self.IBM_SMALL)
        self.save('bigger', {'num_drives': 8})
        code, out, err = run(['preset', 'list'])
        self.assertEqual(code, 0)
        row = next(line for line in out.splitlines()
                   if line.startswith('bigger'))
        self.assertIn('not usable yet', row)
        self.assertIn('names no profile', row)

    def test_list_with_none_defined_names_the_file_and_how_to_start(self):
        code, out, err = run(['preset', 'list'])
        self.assertEqual(code, 0)
        self.assertIn('No presets are defined', out)
        self.assertIn('presets.toml', err)
        self.assertIn('mhvtl preset set NAME --profile IBM', out)

    # -- show, as a concept rather than as a file -------------------------

    def test_show_says_what_it_fixes_and_what_it_leaves_open(self):
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'show', 'ibm-small'])
        self.assertEqual(code, 0)
        self.assertIn('fixed here', out)
        self.assertIn('03584L32', out)
        self.assertIn('2 drives', out)
        self.assertIn('left to IBM', out)
        self.assertIn('D.02', out)          # the revision it does not fix

    def test_show_of_a_vendor_only_preset_is_a_whole_configuration(self):
        """One line as a file, a whole configuration as a concept. It used to
        print the one line, which is the half nobody needs."""
        self.save('ibm-default', {'profile': 'IBM'})
        code, out, err = run(['preset', 'show', 'ibm-default'])
        self.assertEqual(code, 0)
        self.assertIn('fixed here   IBM', out)
        for expected in ('03584L32', 'ULT3580-TD8', 'LTO8', '4 drives',
                         '50 tapes'):
            self.assertIn(expected, out, f'{expected} is not named')

    def test_show_keeps_valid_and_usable_apart(self):
        """A half-built preset is perfectly valid and simply not finished.
        One word for both would make it sound broken."""
        self.save('bigger', {'num_drives': 8})
        code, out, err = run(['preset', 'show', 'bigger'])
        self.assertEqual(code, 0)
        self.assertIn('not finished', out)
        self.assertIn('valid   yes', out)
        self.assertIn('usable  no', out)
        self.assertIn('8 drives', out)

    def test_show_speaks_the_words_that_are_typed(self):
        """`drives`, not `num_drives`: the file and the command line use the
        same vocabulary, and the specification's names are the service's."""
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'show', 'ibm-small'])
        self.assertNotIn('num_drives', out)
        self.assertNotIn('media_count', out)

    def test_show_refuses_a_mistyped_name_with_the_ones_that_exist(self):
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'show', 'ibm-smal'])
        self.assertEqual(code, 1)
        self.assertIn("no preset called 'ibm-smal'", err)
        self.assertIn('ibm-small', err)

    # -- set, unset, delete -----------------------------------------------

    def test_set_builds_one_up_a_piece_at_a_time(self):
        code, out, err = run(['preset', 'set', 'built', '--drives', '2'])
        self.assertEqual(code, 0)
        self.assertIn('not usable yet', err)
        code, out, err = run(['preset', 'set', 'built', '--profile', 'IBM'])
        self.assertEqual(code, 0)
        resolved = self.operations.resolve('built')
        self.assertTrue(resolved.success, resolved.errors)
        self.assertEqual(resolved.data['spec']['num_drives'], 2)

    def test_set_with_no_options_says_what_it_takes(self):
        code, out, err = run(['preset', 'set', 'nothing'])
        self.assertEqual(code, 1)
        self.assertIn('nothing to set', err)
        self.assertIn('--profile', err)

    def test_delete_removes_the_whole_preset(self):
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'delete', 'ibm-small'])
        self.assertEqual(code, 0)
        self.assertEqual(self.operations.names().data['presets'], [])

    def test_unset_removes_one_setting_and_keeps_the_preset(self):
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'unset', 'ibm-small', 'empty_slots'])
        self.assertEqual(code, 0)
        spec = self.operations.describe('ibm-small').data['spec']
        self.assertNotIn('empty_slots', spec)
        self.assertIn('library_model', spec)

    def test_rename_moves_a_preset_and_the_old_name_is_gone(self):
        self.save('ibm-small', self.IBM_SMALL)
        code, out, err = run(['preset', 'rename', 'ibm-small', 'lab-small'])
        self.assertEqual(code, 0, err)
        self.assertEqual([row['name'] for row
                          in self.operations.names().data['presets']],
                         ['lab-small'])
        self.assertEqual(
            self.operations.describe('lab-small').data['spec'],
            self.IBM_SMALL)

    def test_rename_refuses_a_name_that_is_taken_and_says_so(self):
        self.save('one', {'profile': 'IBM'})
        self.save('two', {'profile': 'STK'})
        code, out, err = run(['preset', 'rename', 'one', 'two'])
        self.assertEqual(code, 1)
        self.assertIn('already exists', err)
        self.assertEqual(len(self.operations.names().data['presets']), 2)

    def test_rename_needs_both_names(self):
        """`preset rename OLD` with nothing to rename it to is argparse's to
        refuse, not something to guess at."""
        self.save('one', {'profile': 'IBM'})
        with self.assertRaises(SystemExit) as exit_code:
            run(['preset', 'rename', 'one'])
        self.assertEqual(exit_code.exception.code, 2)

    def test_unset_needs_a_key_so_it_cannot_delete_by_accident(self):
        """Removing the whole preset used to be `unset NAME` with no keys:
        one verb doing two jobs, and the destructive one needed less typing
        than the careful one."""
        self.save('ibm-small', self.IBM_SMALL)
        with self.assertRaises(SystemExit) as exit_code:
            run(['preset', 'unset', 'ibm-small'])
        self.assertEqual(exit_code.exception.code, 2)
        self.assertTrue(self.operations.names().data['presets'])

    # -- mixed drives and media, by the same flags `create` takes ----------

    def test_set_takes_the_same_drive_and_media_flags_as_create(self):
        """A preset reads as the command it replaces, so the flag that builds
        a mixed library is the flag that writes one down."""
        code, out, err = run(['preset', 'set', 'lib-ten', '--profile', 'IBM',
                              '--model', '03584L32',
                              '--drive', 'ULT3580-TD8:2',
                              '--drive', 'ULT3580-TD6:2',
                              '--media', 'LTO8:20', '--media', 'LTO6:10'])
        self.assertEqual(code, 0, err)
        spec = self.operations.describe('lib-ten').data['spec']
        self.assertEqual(spec['drive'], [{'model': 'ULT3580-TD8', 'count': 2},
                                         {'model': 'ULT3580-TD6', 'count': 2}])
        self.assertEqual(spec['media'], [{'density': 'LTO8', 'count': 20},
                                         {'density': 'LTO6', 'count': 10}])

    def test_add_drive_keeps_what_is_there_and_drive_replaces_it(self):
        """Building one up a piece at a time is the point of a preset, and a
        mistake has to be correctable without deleting the whole thing."""
        run(['preset', 'set', 'lib-ten', '--profile', 'IBM',
             '--add-drive', 'ULT3580-TD8:2'])
        code, out, err = run(['preset', 'set', 'lib-ten',
                              '--add-drive', 'ULT3580-TD6:2'])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [run['model'] for run
             in self.operations.describe('lib-ten').data['spec']['drive']],
            ['ULT3580-TD8', 'ULT3580-TD6'])

        code, out, err = run(['preset', 'set', 'lib-ten',
                              '--drive', 'ULT3580-TD9:4'])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.operations.describe('lib-ten').data['spec']['drive'],
            [{'model': 'ULT3580-TD9', 'count': 4}])

    def test_set_refuses_a_count_beside_a_list(self):
        code, out, err = run(['preset', 'set', 'lib-ten', '--profile', 'IBM',
                              '--drive', 'ULT3580-TD8:2', '--drives', '4'])
        self.assertEqual(code, 1)
        self.assertIn('cannot be combined with --drives', err)
        self.assertEqual(self.operations.names().data['presets'], [],
                         'nothing should have been written')

    def test_set_refuses_adding_and_replacing_the_same_list_at_once(self):
        code, out, err = run(['preset', 'set', 'lib-ten', '--profile', 'IBM',
                              '--drive', 'ULT3580-TD8:2',
                              '--add-drive', 'ULT3580-TD6:2'])
        self.assertEqual(code, 1)
        self.assertIn('--add-drive', err)
        self.assertIn('replaces the list', err)

    def test_unset_removes_a_whole_list(self):
        """`unset lib-ten drive` is how a mixed preset becomes a plain one
        again - the keys are the ones `show` prints."""
        self.save('lib-ten', {'profile': 'IBM',
                              'drive': [{'model': 'ULT3580-TD8', 'count': 2}]})
        code, out, err = run(['preset', 'unset', 'lib-ten', 'drive'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('drive',
                         self.operations.describe('lib-ten').data['spec'])

    def test_show_spells_a_mixed_preset_as_the_kinds_it_holds(self):
        self.save('lib-ten', {'profile': 'IBM', 'library_model': '03584L32',
                              'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                                        {'model': 'ULT3580-TD6', 'count': 2}],
                              'media': [{'density': 'LTO8', 'count': 20},
                                        {'density': 'LTO6', 'count': 10}]})
        code, out, err = run(['preset', 'show', 'lib-ten'])
        self.assertEqual(code, 0, err)
        self.assertIn('2 x ULT3580-TD8 + 2 x ULT3580-TD6', out)
        self.assertIn('20 x LTO8 + 10 x LTO6', out)
        # The lists have settled these, so neither half may report them as
        # left to the profile: it read "4 drives, 30 tapes" under "left to
        # IBM" for a preset asking for four drives and thirty cartridges.
        left = out.split('left to IBM', 1)[1]
        for implied in ('drives', 'tapes', 'drive model', 'media type'):
            self.assertNotIn(implied, left,
                             f'{implied} is settled by a list')
