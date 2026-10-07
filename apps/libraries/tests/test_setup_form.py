"""`setup_form.state`: the create-a-library form, decided in the service.

The form's JavaScript used to narrow its own dropdowns and choose its own
selected option, and it drifted from the services three times - it filtered on
what a drive loads instead of what it writes, it chose a half-height HH9 for
LTO-9 where the services choose the full-height TD9, and it reset the drive
count over a preset's own value. These tests are about the answer that
replaces all of that: the options, the selection, the limits and the words.

Nothing here is a new rule, so the tests are about the composition: that the
narrowing is the catalogue's, the counts are the profile's capped by the
host's, the serial is what creation will really write, and the sentences are
finished rather than assembled by a caller.

Every test works on a copy of the captured device.conf, never on /etc/mhvtl:
the limits depend on how many SCSI targets are free, which is a property of
the host's configuration.
"""
import shutil
from pathlib import Path

from .base import TestCase

from apps.libraries.services.config import device_conf as device_conf_format
from apps.libraries.services.config import ids
from apps.libraries.services.libraries import setup_form
from apps.libraries.services.profiles import catalogue, personalities

FIXTURES = Path(__file__).parent / 'fixtures'


class SetupFormTestCase(TestCase):
    """A scratch /etc/mhvtl with the captured four libraries in it."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def state(self, profile='IBM', **asked):
        result = setup_form.state(profile, config_dir=self.config,
                                  **{'library_id': 90, **asked})
        self.assertTrue(result.success, result.message)
        return result.data

    def conf(self):
        return device_conf_format.parse(
            (self.config / 'device.conf').read_text())

    # The form holds a row per kind of drive and per kind of tape, so these
    # read the first row - which is the whole form for the single-kind case
    # every test below the row tests is about.

    def drive(self, answer, row=0):
        return answer['drives']['rows'][row]['selected']

    def density(self, answer, row=0):
        return answer['media']['rows'][row]['selected']

    def options(self, answer, kind, row=0):
        return answer[kind]['rows'][row]['options']

    def kinds(self, answer, kind):
        """[(what, how many), ...] - every row of one section."""
        return [(row['selected'], row['count']) for row in answer[kind]['rows']]


class WhatIsOfferedTests(SetupFormTestCase):

    def test_every_dropdown_comes_back_marked(self):
        """Every option carries whether it is the selected one, so the
        template renders `selected` instead of a script choosing it."""
        answer = self.state()
        offered = [('wanted_media', answer['wanted_media']),
                   ('library_model', answer['library_model'])]
        offered += [(f'{kind} row {index}', row)
                    for kind in ('drives', 'media')
                    for index, row in enumerate(answer[kind]['rows'])]
        for field, select in offered:
            with self.subTest(field=field):
                self.assertTrue(select['options'], f'{field} offers nothing')
                marked = [option for option in select['options']
                          if option['selected']]
                self.assertEqual(len(marked), 1,
                                 f'{field} has {len(marked)} selected options')
                self.assertEqual(marked[0]['value'], select['selected'])

    def test_the_models_are_the_catalogues_in_its_own_order(self):
        answer = self.state()
        self.assertEqual([option['value']
                          for option in answer['library_model']['options']],
                         [row['model']
                          for row in catalogue.describe('IBM')['models']])

    def test_the_drives_are_the_ones_the_chosen_model_takes(self):
        answer = self.state(library_model='3573-TL')
        from apps.libraries.services.profiles import data

        self.assertEqual([option['value']
                          for option in self.options(answer, 'drives')],
                         data.get_valid_drives_for_library('IBM', '3573-TL'))

    def test_the_densities_are_the_ones_the_chosen_drive_loads(self):
        """Loads, not writes: a library that only restores is a real thing to
        build, so a read-only generation is offered and labelled."""
        answer = self.state(drive_model='ULT3580-TD3')
        offered = [option['value'] for option in self.options(answer, 'media')]
        self.assertIn('LTO1', offered)
        read_only = next(option for option in self.options(answer, 'media')
                         if option['value'] == 'LTO1')
        self.assertIn('read-only', read_only['label'])

    def test_a_density_label_carries_its_barcode_suffix(self):
        """MHVTL reads the density out of the suffix and nothing else, so it
        is the part of a cartridge's name an operator has to recognise."""
        answer = self.state(drive_model='ULT3580-TD8')
        label = next(option['label'] for option in self.options(answer, 'media')
                     if option['value'] == 'LTO8')
        self.assertEqual(label, 'LTO8 (suffix: L8)')

    def test_the_tape_filter_offers_every_density_the_vendor_takes(self):
        """It must keep offering all of them even while one is chosen, or the
        operator could not change their mind. The first option is "any",
        which is a real answer and is where the form starts."""
        answer = self.state(wanted_media='LTO9')
        from apps.libraries.services.profiles.data import get_profile_options

        offered = answer['wanted_media']['options']
        self.assertEqual(offered[0]['value'], '')
        self.assertIn('Any tape', offered[0]['label'])
        self.assertEqual([option['value'] for option in offered[1:]],
                         catalogue.densities_of(
                             get_profile_options('IBM')['drive_models']))
        self.assertEqual(answer['wanted_media']['selected'], 'LTO9')

    def test_any_tape_is_the_selected_option_until_one_is_asked_for(self):
        answer = self.state()
        self.assertEqual(answer['wanted_media']['selected'], '')
        self.assertTrue(answer['wanted_media']['options'][0]['selected'])

    def test_the_tape_filter_is_labelled_with_the_size_it_will_be_made_at(self):
        """Not with the native capacity, which is what it said until the size
        became a setting - see test_tape_capacity."""
        from apps.libraries.services.tapes import service as tapes

        answer = self.state()
        label = next(option['label']
                     for option in answer['wanted_media']['options']
                     if option['value'] == 'LTO8')
        self.assertEqual(label, tapes.media_label('LTO8'))


class WhatIsSelectedTests(SetupFormTestCase):

    def test_an_unnarrowed_form_takes_the_profiles_defaults(self):
        answer = self.state()
        from apps.libraries.services.profiles.data import get_profile_options

        defaults = get_profile_options('IBM')['defaults']
        self.assertEqual(answer['library_model']['selected'],
                         defaults['library_model'])
        self.assertEqual(self.drive(answer),
                         defaults['drive_model'])
        self.assertEqual(self.density(answer),
                         defaults['media_type'])

    def test_asking_for_a_tape_narrows_the_models_and_the_drives(self):
        answer = self.state(wanted_media='LTO9')
        self.assertEqual(self.density(answer), 'LTO9')
        for option in self.options(answer, 'drives'):
            with self.subTest(drive=option['value']):
                self.assertIn('LTO9', catalogue.writes(option['value']))

    def test_the_drive_for_a_wanted_tape_is_chosen_by_generation(self):
        """The case the page got wrong: it took the last writer listed, which
        is a half-height ULT3580-HH9, where the services choose the
        full-height TD9. Same generation, and not the drive anybody means."""
        answer = self.state(wanted_media='LTO9')
        self.assertEqual(self.drive(answer), 'ULT3580-TD9')
        self.assertEqual(
            self.drive(answer),
            catalogue.default_drive_for('IBM', answer['library_model']['selected'],
                                        media='LTO9'))

    def test_what_the_operator_chose_beats_every_default(self):
        answer = self.state(library_model='3573-TL',
                            drive_model='ULT3580-TD6', media_type='LTO5')
        self.assertEqual(answer['library_model']['selected'], '3573-TL')
        self.assertEqual(self.drive(answer), 'ULT3580-TD6')
        self.assertEqual(self.density(answer), 'LTO5')

    def test_a_preset_is_chosen_where_the_profile_default_would_be(self):
        """The preset goes where the defaults go, so there is no second
        mechanism - and the counts come with it. The page carried the count
        in the HTML and then overwrote it."""
        answer = self.state(preset={'library_model': '3573-TL',
                                    'drive_model': 'ULT3580-TD6',
                                    'media_type': 'LTO6',
                                    'num_drives': 2, 'media_count': 20,
                                    'empty_slots': 4})
        self.assertEqual(answer['library_model']['selected'], '3573-TL')
        self.assertEqual(self.drive(answer), 'ULT3580-TD6')
        self.assertEqual(self.density(answer), 'LTO6')
        self.assertEqual(answer['counts'], {'num_drives': 2,
                                            'media_count': 20,
                                            'empty_slots': 4,
                                            'total_slots': 24})

    def test_a_choice_beats_the_preset_it_started_from(self):
        answer = self.state(drive_model='ULT3580-TD8',
                            preset={'drive_model': 'ULT3580-TD6'})
        self.assertEqual(self.drive(answer), 'ULT3580-TD8')

    def test_a_stale_tape_is_ignored_rather_than_refused(self):
        """?wanted_media= comes from the vendor page's filter links, and an old link
        should open the page rather than fail on it."""
        answer = self.state(wanted_media='T10KC')     # no IBM drive takes it
        self.assertEqual(answer['wanted_media']['selected'], '')
        self.assertTrue(answer['library_model']['options'])

    def test_a_choice_the_narrowing_rules_out_falls_back(self):
        """Not to the last option, which is what the page did: STK's L700
        list ends with a DLT7000."""
        answer = self.state(wanted_media='LTO9', drive_model='ULT3580-TD3')
        self.assertNotEqual(self.drive(answer), 'ULT3580-TD3')
        self.assertEqual(self.drive(answer), 'ULT3580-TD9')


class LimitsTests(SetupFormTestCase):
    """How many drives, and which budget decided."""

    def test_the_limit_is_the_smallest_of_the_three_budgets(self):
        answer = self.state()
        limits = answer['limits']
        conf = self.conf()
        self.assertEqual(limits['free_targets'], ids.free_targets(conf))
        self.assertEqual(limits['max_drives'],
                         min(limits['layout_max_drives'],
                             limits['free_targets'] - 1,
                             limits['free_drive_ids']))

    def test_the_targets_are_usually_what_binds_it(self):
        """An IBM 3584 can address 511 drives and this host has nowhere near
        that many SCSI targets left, so offering 511 would be offering a
        library that cannot be created."""
        answer = self.state()
        self.assertEqual(answer['limits']['layout_max_drives'], 511)
        self.assertEqual(answer['limits']['bound_by'], 'SCSI targets')
        self.assertLess(answer['limits']['max_drives'], 511)

    def test_the_model_binds_it_when_the_model_is_small(self):
        """A Spectra GATOR addresses 32 drives, which is fewer than the
        targets a fresh host has free."""
        answer = self.state('SPECTRA', library_model='GATOR')
        self.assertEqual(answer['limits']['bound_by'], 'the model')
        self.assertEqual(answer['limits']['max_drives'], 32)

    def test_the_reason_is_said_in_words(self):
        answer = self.state()
        self.assertIn('SCSI targets', answer['says']['drives'])
        self.assertIn('one for the library and one per drive',
                      answer['says']['drives'])
        self.assertIn(str(answer['limits']['max_drives']),
                      answer['says']['drives'])

    def test_the_slot_limit_is_the_models(self):
        answer = self.state()
        self.assertEqual(answer['limits']['max_slots'], 64512)
        self.assertIn('64512', answer['says']['slots'])

    def test_a_count_is_capped_by_what_is_possible(self):
        """applyLimits() in the page, except that this caps the value before
        it is rendered rather than after.

        The *row* is capped and not only the total: the rows are what
        creation reads, so a capped total over a row of 900 would have shown
        32 under an input saying 900 and then failed validation on submit.
        """
        answer = self.state('SPECTRA', library_model='GATOR',
                            preset={'num_drives': 900})
        self.assertEqual(answer['counts']['num_drives'], 32)
        self.assertEqual(answer['drives']['rows'][0]['count'], 32)

    def test_a_typed_row_is_capped_the_same_way(self):
        answer = self.state('SPECTRA', library_model='GATOR',
                            drive_runs=['ULT3580-TD8:900'])
        self.assertEqual(answer['drives']['rows'][0]['count'], 32)
        self.assertEqual(answer['counts']['num_drives'], 32)

    def test_a_second_row_gets_what_the_first_left(self):
        """The budget is the library's, not the row's: two rows of 32 in a
        32-drive library is 64 drives, which cannot be created."""
        answer = self.state('SPECTRA', library_model='GATOR',
                            drive_runs=['ULT3580-TD8:30', 'ULT3580-TD7:20'])
        self.assertEqual([row['count'] for row in answer['drives']['rows']],
                         [30, 2])
        self.assertEqual(answer['counts']['num_drives'], 32)

    def test_a_cartridge_row_is_capped_by_the_slots(self):
        most = self.state('SPECTRA', library_model='GATOR')['limits']['max_slots']
        answer = self.state('SPECTRA', library_model='GATOR',
                            media_runs=[f'LTO8:{most + 100}'])
        self.assertEqual(answer['media']['rows'][0]['count'], most)
        self.assertEqual(answer['counts']['empty_slots'], 0)

    def test_the_counts_otherwise_come_from_the_profile(self):
        from apps.libraries.services.profiles.data import get_profile_options

        answer = self.state()
        defaults = get_profile_options('IBM')['defaults']
        self.assertEqual(answer['counts']['num_drives'],
                         defaults['num_drives'])
        self.assertEqual(answer['counts']['media_count'],
                         defaults['media_count'])


class WhatWillBeWrittenTests(SetupFormTestCase):
    """The form shows what creation will really do, by asking it."""

    def test_the_serial_is_the_one_the_library_will_report(self):
        answer = self.state()
        self.assertEqual(answer['serial'], '80000090')
        self.assertEqual(answer['serial'],
                         personalities.device_serial(90))

    def test_the_barcode_example_is_the_prefix_and_the_suffix(self):
        answer = self.state()
        self.assertEqual(answer['barcode_prefix'], 'I90')
        self.assertEqual(answer['media_suffix'], 'L8')
        self.assertEqual(answer['says']['barcode'], 'Example: I90001L8')

    def test_the_product_and_revision_are_the_profiles(self):
        answer = self.state()
        self.assertEqual(answer['product'],
                         answer['library_model']['selected'])
        self.assertTrue(answer['library_revision'])

    def test_an_id_is_offered_when_none_is_given(self):
        """The next free one, by the same rule `library create` uses."""
        result = setup_form.state('IBM', config_dir=self.config)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['library_id'],
                         ids.next_library_id(self.conf()))


class SentenceTests(SetupFormTestCase):
    """Services decide and format: the page concatenates nothing."""

    def test_the_drive_sentence_names_what_mhvtl_will_emulate(self):
        answer = self.state(drive_model='ULT3580-TD8')
        self.assertEqual(
            answer['drives']['rows'][0]['says'],
            'ULT3580-TD8 is emulated as init_ult3580_td8; '
            'it writes LTO8, LTO7.')

    def test_the_drive_sentence_says_what_it_can_only_read(self):
        answer = self.state(drive_model='ULT3580-TD3')
        self.assertIn('only reads LTO1', answer['drives']['rows'][0]['says'])

    def test_a_read_only_choice_is_explained_rather_than_hidden(self):
        answer = self.state(drive_model='ULT3580-TD3', media_type='LTO1')
        self.assertEqual(self.density(answer), 'LTO1')
        self.assertIn('read-only in every drive this library has',
                      answer['says']['media'])
        self.assertIn('restored from but not backed up to',
                      answer['says']['media'])

    def test_a_writable_choice_says_nothing_alarming(self):
        answer = self.state(drive_model='ULT3580-TD8', media_type='LTO8')
        self.assertNotIn('read-only', answer['says']['media'])


class MoreThanOneKindTests(SetupFormTestCase):
    """A row per kind of drive and per kind of tape.

    The form had one dropdown for each, so a library holding two generations
    could be described by a preset, built from the command line, written to
    device.conf and listed by `library list` - and not made here. Worse, a
    preset holding two was opened silently as one.
    """

    MIXED = {'drive_runs': ['ULT3580-TD8:2', 'ULT3580-TD6:2'],
             'media_runs': ['LTO8:20', 'LTO6:10']}

    LIB_TEN = {'profile': 'IBM', 'library_model': '03584L32',
               'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                         {'model': 'ULT3580-TD6', 'count': 2}],
               'media': [{'density': 'LTO8', 'count': 20},
                         {'density': 'LTO6', 'count': 10}]}

    def test_a_row_for_each_kind_in_the_order_asked(self):
        """Slot order, and therefore SCSI target order."""
        answer = self.state(**self.MIXED)
        self.assertEqual(self.kinds(answer, 'drives'),
                         [('ULT3580-TD8', 2), ('ULT3580-TD6', 2)])
        self.assertEqual(self.kinds(answer, 'media'),
                         [('LTO8', 20), ('LTO6', 10)])

    def test_the_counts_are_what_the_rows_add_up_to(self):
        answer = self.state(**self.MIXED)
        self.assertEqual(answer['counts']['num_drives'], 4)
        self.assertEqual(answer['counts']['media_count'], 30)
        self.assertEqual(answer['counts']['total_slots'],
                         30 + answer['counts']['empty_slots'])

    def test_what_it_all_adds_up_to_is_said(self):
        """A mixed library's totals were visible nowhere: 20 LTO8 and 10 LTO6
        made a reader do the addition."""
        said = self.state(**self.MIXED)['says']['holds']
        self.assertIn('4 drive(s) of 2 kind(s)', said)
        self.assertIn('30 cartridge(s)', said)

    def test_a_kind_already_chosen_is_not_offered_again(self):
        """`--drive X:2 --drive X:3` is five drives of X written confusingly,
        and the interactive loop narrows the same way."""
        answer = self.state(**self.MIXED)
        second = [option['value'] for option in self.options(answer, 'drives', 1)]
        self.assertNotIn('ULT3580-TD8', second)
        self.assertIn('ULT3580-TD6', second)
        first = [option['value'] for option in self.options(answer, 'drives', 0)]
        self.assertIn('ULT3580-TD8', first,
                      'a row must keep its own choice in its own options')

    def test_the_densities_are_what_any_of_the_drives_loads(self):
        """An LTO-6 cartridge belongs in a library that has a TD6, even
        though the TD8 beside it cannot read it."""
        answer = self.state(**self.MIXED)
        offered = [option['value'] for option in self.options(answer, 'media')]
        self.assertIn('LTO8', offered)
        self.assertIn('LTO6', offered)

    def test_read_only_means_no_drive_here_writes_it(self):
        """With one drive, LTO4 is read-only in a TD6. With a TD6 and a TD5
        beside each other it is still read-only; with a TD4 it is not."""
        with_td6 = self.state(drive_runs=['ULT3580-TD6:1'],
                              media_runs=['LTO4:1'])
        self.assertTrue(with_td6['media']['rows'][0]['read_only'])
        self.assertIn('read-only', with_td6['says']['media'])

        with_td4 = self.state(drive_runs=['ULT3580-TD6:1', 'ULT3580-TD4:1'],
                              media_runs=['LTO4:1'])
        self.assertFalse(with_td4['media']['rows'][0]['read_only'],
                         'the TD4 writes LTO4, so the library can')
        self.assertNotIn('read-only', with_td4['says']['media'])

    def test_a_row_can_be_removed_only_while_there_are_two(self):
        one = self.state()
        self.assertFalse(one['drives']['rows'][0]['can_remove'])
        two = self.state(**self.MIXED)
        self.assertTrue(all(row['can_remove'] for row in two['drives']['rows']))

    def test_another_kind_can_be_added_until_there_is_nothing_left(self):
        answer = self.state()
        self.assertTrue(answer['drives']['can_add'])
        self.assertEqual(answer['drives']['why_not'], '')

        every = [option['value'] for option in self.options(answer, 'drives')]
        full = self.state(drive_runs=[f'{model}:1' for model in every])
        self.assertFalse(full['drives']['can_add'])
        self.assertIn('already listed', full['drives']['why_not'])

    def test_no_more_kinds_when_there_is_no_room_for_a_drive(self):
        """Adding a kind means adding a drive, and this host has a limit.

        The limit is asked for rather than written here: it depends on how
        many SCSI targets the fixture leaves free, and a number copied into
        a test is the same second answer this whole unit is about.
        """
        most = self.state()['limits']['max_drives']
        answer = self.state(drive_runs=[f'ULT3580-TD8:{most}'])
        self.assertEqual(answer['counts']['num_drives'],
                         answer['limits']['max_drives'])
        self.assertFalse(answer['drives']['can_add'])
        self.assertIn('no room for another', answer['drives']['why_not'])

    def test_a_mixed_preset_fills_the_rows(self):
        """The defect this unit exists for. The form had one dropdown each,
        so lib-ten opened as ULT3580-TD8 and LTO8 with the vendor's default
        counts - 4 drives and 50 cartridges where the preset says 4 and 30 -
        under a message saying the preset had been applied."""
        answer = self.state(preset=self.LIB_TEN)
        self.assertEqual(self.kinds(answer, 'drives'),
                         [('ULT3580-TD8', 2), ('ULT3580-TD6', 2)])
        self.assertEqual(self.kinds(answer, 'media'),
                         [('LTO8', 20), ('LTO6', 10)])
        self.assertEqual(answer['counts']['media_count'], 30)

    def test_the_rows_beat_the_preset_they_started_from(self):
        answer = self.state(preset=self.LIB_TEN,
                            drive_runs=['ULT3580-TD9:1'])
        self.assertEqual(self.kinds(answer, 'drives'), [('ULT3580-TD9', 1)])

    def test_the_nominal_density_is_the_first_one_a_drive_writes(self):
        """What the barcode suffix and `tape media` report for a mixed
        library - a rule of its own, asked of apply_defaults rather than
        picked here."""
        answer = self.state(**self.MIXED)
        self.assertEqual(answer['media_type'], 'LTO8')
        self.assertEqual(answer['media_suffix'], 'L8')
        self.assertIn('001L8', answer['says']['barcode'])

    def test_the_specification_it_would_create_carries_the_lists(self):
        """And not the counts they imply: a count beside a list is refused by
        the file format, by the command line and by validation."""
        answer = self.state(**self.MIXED)
        asked = setup_form.as_spec('IBM', 90,
                                   answer['library_model']['selected'],
                                   answer['drives'], answer['media'],
                                   answer['counts'])
        self.assertEqual(asked['drive'],
                         [{'model': 'ULT3580-TD8', 'count': 2},
                          {'model': 'ULT3580-TD6', 'count': 2}])
        self.assertEqual(asked['media'],
                         [{'density': 'LTO8', 'count': 20},
                          {'density': 'LTO6', 'count': 10}])
        self.assertNotIn('num_drives', asked)
        self.assertNotIn('media_count', asked)

    def test_one_kind_still_collapses_to_what_it_always_was(self):
        answer = self.state(drive_model='ULT3580-TD8', num_drives=2,
                            media_type='LTO8', media_count=20)
        asked = setup_form.as_spec('IBM', 90, '03584L32', answer['drives'],
                                   answer['media'], answer['counts'])
        self.assertEqual(asked['drive'], [{'model': 'ULT3580-TD8', 'count': 2}])
        self.assertEqual(asked['media'], [{'density': 'LTO8', 'count': 20}])

    def test_a_row_that_cannot_be_read_is_refused_with_the_form(self):
        refused = setup_form.state('IBM', config_dir=self.config,
                                   drive_runs=['ULT3580-TD8:x'])
        self.assertFalse(refused.success)
        self.assertIn('not a kind and a count', refused.message)
        self.assertTrue(any('MODEL:COUNT' in error for error in refused.errors))


class RefusalTests(SetupFormTestCase):

    def test_an_unknown_vendor_is_refused_with_the_ones_that_exist(self):
        result = setup_form.state('acme', config_dir=self.config)
        self.assertFalse(result.success)
        self.assertIn('not a vendor profile', result.message)
        self.assertTrue(any('IBM' in error for error in result.errors))

    def test_a_host_with_no_device_conf_is_not_an_error(self):
        """The first library on a host is created before the file exists."""
        result = setup_form.state('IBM', config_dir=self.tmpdir())
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['library_id'], ids.LIBRARY_STEP)
        self.assertEqual(result.data['limits']['free_targets'],
                         device_conf_format.MAX_TARGET + 1)
