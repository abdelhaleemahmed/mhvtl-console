"""`mhvtl library create --interactive`: steps 9 and 10 of the catalogue plan.

The loop is a form, not a brain. It asks questions and calls six things, all
of which existed before it - the catalogue for the options, apply_defaults for
the defaults, lifecycle.preview for the check, presets.save to keep it,
create_library_workflow to build it, next_id for the id. These tests are about
the asking: the order, what is offered, what the defaults are, and that
quitting leaves nothing behind.

The last class is step 10, and it is the point of the whole exercise: **the
terminal and the setup form have to offer the same things for the same
profile.** It compares what the loop offered, question by question, against
the JSON the web page was handed. If either front end ever starts composing
its own answer, those two stop matching.

Nothing here needs a terminal: Prompt(answers=[...]) replaces the reader, so
the whole conversation runs from a list of strings. The alternative - patching
builtins.input - replaces a global for every other test in the run.
"""
import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from .base import TestCase

from mhvtl_cli import main, prompt
from mhvtl_cli.commands import library as command

def answers(*, profile='IBM', model='', drive='', drives='', more_drives='',
            media='', tapes='', more_media='', empty='', library_id='',
            then=('q',)):
    """One answer per question, in the order the loop asks them.

    Named rather than counted, because the order has changed once already and
    a list of nine empty strings says nothing about which question each one
    answers. An empty answer takes the default, and the default for "add
    another kind?" is no - so this is the single-drive, single-density path
    unless a test says otherwise.

    The questions, in order: the vendor, one of its models, a drive that model
    takes, how many of it, whether there is another kind of drive, a density
    that drive writes, how many cartridges, whether there is another density,
    the empty slots, the id - then the menu at the end.
    """
    return [profile, model, drive, drives, more_drives,
            media, tapes, more_media, empty, library_id, *then]


#: The whole conversation, taking every default, ending in quit.
TO_THE_END = answers(drives='2', tapes='20')


def args_for(*extra, before=()):
    """A real parsed Namespace, so the test cannot drift from the parser.

    ``before`` is for the global flags - --json and --quiet belong to the top
    level, so `mhvtl --json library create` parses and
    `mhvtl library create --json` does not.
    """
    return main.build_parser().parse_args(
        [*before, 'library', 'create', '--interactive', *extra])


def conversation(answers):
    """Run the loop with these answers. Returns (result, the prompt)."""
    asker = prompt.Prompt(answers=answers, out=io.StringIO())
    result = command._ask_for_a_library(args_for(), asker=asker)
    return result, asker


def asked_about(asker, question):
    """The one recorded question whose text starts with this."""
    for entry in asker.asked:
        if entry['question'].startswith(question):
            return entry
    raise AssertionError(f'{question!r} was never asked; '
                         f'asked: {[e["question"] for e in asker.asked]}')


def every_time_asked(asker, question):
    """Every recorded question starting with this, in order.

    A mixed library is asked for its drive model more than once - one kind at
    a time - so a test about the second kind cannot take the first match.
    """
    return [entry for entry in asker.asked
            if entry['question'].startswith(question)]


class QuittingTests(TestCase):

    def test_quitting_returns_nothing_and_writes_nothing(self):
        """Nothing is written until the final choice, which is also what
        makes the preview free."""
        result, asker = conversation(TO_THE_END)
        self.assertIsNone(result)

    def test_running_out_of_answers_is_an_interrupt_not_a_crash(self):
        """A Ctrl-D in a real session, and an under-answered test here."""
        result, asker = conversation(['IBM', ''])
        self.assertIsNone(result)

    def test_an_interrupt_says_that_nothing_was_written(self):
        out = io.StringIO()
        asker = prompt.Prompt(answers=['IBM'], out=out)
        command._ask_for_a_library(args_for(), asker=asker)
        self.assertIn('Nothing was written', out.getvalue())


class QuestionTests(TestCase):
    """What is asked, in what order, with what on offer."""

    def test_the_order_is_the_narrowing_order(self):
        """A property of the data rather than a decision here: a vendor, then
        one of its models, then a drive that model takes and how many of it,
        then a density that drive writes and how many cartridges, then what is
        left of the slots.

        Each count follows its own kind rather than coming at the end, which
        is what asking one kind at a time means: "how many drives" after a
        second drive model would otherwise be ambiguous about which.
        """
        result, asker = conversation(TO_THE_END)
        self.assertEqual([entry['question'] for entry in asker.asked][:10], [
            'Vendor profile', 'Library model',
            'Drive model', 'How many drives', 'Add another drive type?',
            'Cartridge density', 'How many cartridges', 'Add another density?',
            'Empty slots', 'Library id'])

    def test_the_first_question_has_no_default(self):
        """Nine real catalogues and no function that prefers one, so a
        default would be this file choosing a favourite."""
        result, asker = conversation(TO_THE_END)
        self.assertIsNone(asked_about(asker, 'Vendor profile')['default'])

    def test_every_later_question_has_one(self):
        result, asker = conversation(TO_THE_END)
        for entry in asker.asked[1:10]:
            with self.subTest(question=entry['question']):
                self.assertIsNotNone(entry['default'],
                                     f"{entry['question']} offers no default")

    def test_a_drive_the_model_cannot_take_is_never_offered(self):
        """Narrowing beats refusing: the teaching refusals are the fallback
        for the scripted path, not the main event here."""
        from apps.libraries.services.profiles import data

        result, asker = conversation(answers(model='3573-TL', drives='2',
                                             tapes='20'))
        offered = asked_about(asker, 'Drive model')['options']
        self.assertEqual(
            offered, data.get_valid_drives_for_library('IBM', '3573-TL'))
        self.assertNotIn('03592J1A', offered)

    def test_only_densities_the_drive_can_write_are_offered(self):
        """A drive that merely loads one would give a library that can be
        restored from and not backed up to."""
        from apps.libraries.services.profiles import catalogue

        result, asker = conversation(answers(drive='ULT3580-TD3', drives='2',
                                             tapes='20'))
        offered = asked_about(asker, 'Cartridge density')['options']
        self.assertEqual(offered, catalogue.writes('ULT3580-TD3'))
        self.assertNotIn('LTO1', offered)      # TD3 reads LTO1, cannot write

    def test_a_read_only_density_is_mentioned_rather_than_hidden(self):
        out = io.StringIO()
        asker = prompt.Prompt(answers=answers(drive='ULT3580-TD3', drives='2',
                                              tapes='20'), out=out)
        command._ask_for_a_library(args_for(), asker=asker)
        self.assertIn('also loads LTO1', out.getvalue())
        self.assertIn('cannot write', out.getvalue())

    def test_the_counts_are_clamped_to_the_models_limits(self):
        """The limits are the service's - a model's max_drives comes from
        profiles/personalities - so the refusal can say what the number has
        to be rather than only that it was wrong."""
        out = io.StringIO()
        # Refused and asked again, so the count is answered twice: the
        # rejected answer goes in before the one that is taken.
        script = answers(drives='2', tapes='20')
        script.insert(script.index('2'), '99999')
        asker = prompt.Prompt(answers=script, out=out)
        command._ask_for_a_library(args_for(), asker=asker)
        self.assertIn('more than the maximum', out.getvalue())

    def test_nonsense_is_asked_again(self):
        out = io.StringIO()
        script = answers(drives='2', tapes='20')
        script.insert(0, 'nosuchvendor')
        asker = prompt.Prompt(answers=script, out=out)
        command._ask_for_a_library(args_for(), asker=asker)
        self.assertIn('is not one of them', out.getvalue())


class TheAnswersBecomeASpecTests(TestCase):

    def test_empty_answers_give_the_profiles_own_configuration(self):
        from apps.libraries.services.profiles.data import get_profile_options

        asker = prompt.Prompt(answers=answers(then=('c',)),
                              out=io.StringIO())
        spec, keep = command._ask_for_a_library(args_for(), asker=asker)
        defaults = get_profile_options('IBM')['defaults']

        self.assertIsNone(keep)
        self.assertEqual(spec['profile'], 'IBM')
        self.assertEqual(spec['library_model'], defaults['library_model'])
        self.assertEqual(spec['drive_model'], defaults['drive_model'])
        self.assertEqual(spec['media_type'], defaults['media_type'])
        self.assertEqual(spec['num_drives'], defaults['num_drives'])
        self.assertEqual(spec['media_count'], defaults['media_count'])
        self.assertEqual(spec['empty_slots'], defaults['empty_slots'])

    def test_what_was_typed_is_what_comes_back(self):
        asker = prompt.Prompt(
            answers=answers(profile='STK', model='SL500', drive='T10000C',
                            drives='3', media='T10KB', tapes='12', empty='5',
                            library_id='77', then=('c',)),
            out=io.StringIO())
        spec, keep = command._ask_for_a_library(args_for(), asker=asker)
        self.assertEqual(spec, {'profile': 'STK', 'library_model': 'SL500',
                                'drive_model': 'T10000C',
                                'media_type': 'T10KB', 'num_drives': 3,
                                'media_count': 12, 'empty_slots': 5,
                                'library_id': 77})

    def test_the_spec_is_one_apply_defaults_completes(self):
        """The loop fills in a specification; it is not a second way of
        creating a library. What it hands back goes through the same
        create_library_workflow as the flag-driven path."""
        from apps.libraries.services.libraries import spec as spec_rules

        asker = prompt.Prompt(answers=answers(drives='2', tapes='20',
                                              then=('c',)),
                              out=io.StringIO())
        spec, keep = command._ask_for_a_library(args_for(), asker=asker)
        filled = spec_rules.apply_defaults(dict(spec))
        self.assertEqual(filled['product'], spec['library_model'])
        self.assertEqual(filled['drive_product'], spec['drive_model'])
        self.assertEqual(filled['num_drives'], 2)

    def test_keeping_it_asks_for_a_name(self):
        asker = prompt.Prompt(answers=answers(drives='2', tapes='20',
                                              then=('s', 'lab-small')),
                              out=io.StringIO())
        spec, keep = command._ask_for_a_library(args_for(), asker=asker)
        self.assertEqual(keep, 'lab-small')

    def test_a_vendors_name_is_refused_before_the_library_is_created(self):
        """The service refuses it too, but asking again beats creating the
        library and then reporting that the preset was not kept."""
        out = io.StringIO()
        asker = prompt.Prompt(
            answers=answers(drives='2', tapes='20',
                            then=('s', 'IBM', 'ibm-small')), out=out)
        spec, keep = command._ask_for_a_library(args_for(), asker=asker)
        self.assertEqual(keep, 'ibm-small')
        self.assertIn('is a vendor catalogue', out.getvalue())

    def test_the_preview_prints_the_configuration_and_returns_to_the_menu(self):
        """On stdout, because it is data: `--interactive > my.conf` keeps the
        questions on the terminal and the configuration in the file."""
        asker = prompt.Prompt(answers=answers(drives='2', tapes='20',
                                              then=('p', 'q')),
                              out=io.StringIO())
        printed = io.StringIO()
        with redirect_stdout(printed):
            result = command._ask_for_a_library(args_for(), asker=asker)
        self.assertIsNone(result)
        self.assertIn('Library: ', printed.getvalue())
        self.assertIn('ULT3580-TD8', printed.getvalue())


class MoreThanOneKindTests(TestCase):
    """Two kinds of drive and two of cartridge, asked one kind at a time.

    The loop asks for a model and a count, then whether there is another, and
    stops offering what has already been chosen. What comes back is the same
    list `--drive ULT3580-TD8:2 --drive ULT3580-TD6:2` builds and the same one
    a preset's [[name.drive]] array parses to, so the three ways of asking for
    a mixed library meet here and go on as one.
    """

    #: Two LTO-8 drives and two LTO-6, with cartridges for both. Written out
    #: rather than built, because the point of it is the second round: a
    #: model, a count, "another?" - then the same three again.
    MIXED = ['IBM', '03584L32',
             'ULT3580-TD8', '2', 'y',     # the first kind, and there is more
             'ULT3580-TD6', '2', '',      # the second, and no more
             'LTO8', '3', 'y',            # cartridges for the first
             'LTO6', '2', '',             # and for the second
             '', '']                      # the empty slots and the id

    def _ask(self, *then):
        out = io.StringIO()
        asker = prompt.Prompt(answers=[*self.MIXED, *then], out=out)
        asked = command._ask_for_a_library(args_for(), asker=asker)
        return asked, asker, out

    def test_the_answers_come_back_as_the_lists_the_services_take(self):
        (spec, keep), asker, out = self._ask('c')
        self.assertEqual(spec['drive'], [{'model': 'ULT3580-TD8', 'count': 2},
                                         {'model': 'ULT3580-TD6', 'count': 2}])
        self.assertEqual(spec['media'], [{'density': 'LTO8', 'count': 3},
                                         {'density': 'LTO6', 'count': 2}])

    def test_the_counts_a_list_implies_are_not_also_stored(self):
        """A count beside a list is two answers to one question, and the file
        format refuses the pair - so the loop must not compose it."""
        (spec, keep), asker, out = self._ask('c')
        for implied in ('num_drives', 'drive_model',
                        'media_count', 'media_type'):
            self.assertNotIn(implied, spec)

    def test_what_is_already_chosen_is_not_offered_again(self):
        _asked, asker, _out = self._ask('q')
        second = every_time_asked(asker, 'Drive model')[1]
        self.assertNotIn('ULT3580-TD8', second['options'])
        self.assertIn('ULT3580-TD6', second['options'])

    def test_only_the_first_of_each_kind_is_offered_a_default(self):
        """The profile's answer is already in the first, and repeating it
        would suggest that asking twice was pointless."""
        _asked, asker, _out = self._ask('q')
        for question in ('Drive model', 'How many drives',
                         'Cartridge density', 'How many cartridges'):
            with self.subTest(question=question):
                rounds = every_time_asked(asker, question)
                self.assertIsNotNone(rounds[0]['default'])
                self.assertIsNone(rounds[1]['default'])

    def test_a_density_the_second_drive_writes_is_offered(self):
        """LTO6 is on the list because the TD6 writes it, even though the TD8
        beside it cannot read it - the rule the services apply to a mixed
        library, applied here by asking them."""
        _asked, asker, _out = self._ask('q')
        offered = every_time_asked(asker, 'Cartridge density')[0]['options']
        self.assertIn('LTO8', offered)
        self.assertIn('LTO6', offered)
        self.assertNotIn('LTO1', offered)      # nothing here writes it

    def test_the_summary_says_both_kinds_and_that_it_is_valid(self):
        _asked, asker, out = self._ask('q')
        said = out.getvalue()
        self.assertIn('2 x ULT3580-TD8, 2 x ULT3580-TD6', said)
        self.assertIn('3 x LTO8, 2 x LTO6', said)
        self.assertIn('checked: valid', said)

    def test_what_it_hands_back_fills_in_as_the_library_asked_for(self):
        """The loop fills in a specification and nothing more: the same
        apply_defaults the flag-driven path uses turns this into slots."""
        from apps.libraries.services.libraries import spec as spec_rules

        (spec, keep), asker, out = self._ask('c')
        filled = spec_rules.apply_defaults({**spec, 'library_id': 40})
        self.assertEqual([slot['product'] for slot in filled['drive_slots']],
                         ['ULT3580-TD8', 'ULT3580-TD8',
                          'ULT3580-TD6', 'ULT3580-TD6'])
        self.assertEqual([(run['density'], run['count'])
                          for run in filled['media_runs']],
                         [('LTO8', 3), ('LTO6', 2)])

    def test_one_kind_still_comes_back_as_the_simple_pair(self):
        """A preset saved from a single-drive library should read as the
        command somebody would have typed, not as a list of one."""
        asker = prompt.Prompt(answers=answers(drives='2', tapes='20',
                                              then=('c',)),
                              out=io.StringIO())
        spec, _keep = command._ask_for_a_library(args_for(), asker=asker)
        self.assertNotIn('drive', spec)
        self.assertNotIn('media', spec)
        self.assertEqual(spec['num_drives'], 2)
        self.assertEqual(spec['media_count'], 20)


class OptingInTests(TestCase):
    """Opt in, never inferred - and refused rather than surprising."""

    def refusal(self, *extra, terminal=True, before=()):
        """(exit code, what it said on stderr)."""
        said = io.StringIO()
        with mock.patch('mhvtl_cli.prompt.require_terminal',
                        return_value=terminal):
            with redirect_stderr(said):
                code = command._only_interactive(args_for(*extra,
                                                          before=before))
        return code, said.getvalue()

    def test_it_refuses_to_start_without_a_terminal(self):
        """A cron entry that runs this by mistake has to fail at once rather
        than wait for an answer that will never come."""
        code, said = self.refusal(terminal=False)
        self.assertEqual(code, 1)
        self.assertIn('needs a terminal', said)
        self.assertIn('--profile IBM --drives 2', said)   # what to do instead

    def test_it_combines_with_nothing_that_answers_a_question(self):
        for flag, value in (('--drives', '2'), ('--model', '03584L32'),
                            ('--tapes', '20'), ('--id', '90'),
                            ('--media-type', 'LTO8'), ('--empty-slots', '4'),
                            ('--drive-model', 'ULT3580-TD8'),
                            ('--serial', 'X'), ('--drive-revision', 'HB81'),
                            ('--save-preset', 'x')):
            with self.subTest(flag=flag):
                code, said = self.refusal(flag, value)
                self.assertEqual(code, 1, f'{flag} was allowed')
                self.assertIn(flag, said)
                self.assertIn('would quietly overrule an answer', said)

    def test_dry_run_is_refused_because_the_menu_has_a_preview(self):
        code, said = self.refusal('--dry-run')
        self.assertEqual(code, 1)
        self.assertIn('--dry-run', said)

    def test_json_is_refused_because_a_prompt_loop_cannot_emit_it(self):
        code, said = self.refusal(before=('--json',))
        self.assertEqual(code, 1)
        self.assertIn('--json', said)

    def test_no_start_and_no_media_are_allowed(self):
        """They are about what happens after the specification is settled."""
        code, said = self.refusal('--no-start', '--no-media')
        self.assertIsNone(code)
        self.assertEqual(said, '')


class SameOptionsAsTheFormTests(TestCase):
    """Step 10: the interactive path and the setup wizard offer the same
    options for the same profile.

    This is the test the whole plan was ordered around. The loop and the form
    ask the same questions in the same order, and if either one ever works out
    an answer for itself instead of asking the catalogue, these stop matching.

    The form was compared through the JSON it embedded, because its script
    narrowed the dropdowns itself. Since 4 October 2026 the server decides the
    form and the page renders it, so the comparison is against that one
    answer - ``libraries.setup_form.state`` - which is what the browser is
    showing. Agreeing with it is agreeing with the page.
    """

    def form(self, brand, **asked):
        from apps.libraries.services.libraries import setup_form

        answer = setup_form.state(brand, **asked)
        self.assertTrue(answer.success, answer.message)
        return answer.data

    def offered(self, form, field):
        """The dropdown's values, without the "any tape" option.

        The form holds a row per kind of drive and of tape now, so the
        single-kind question these tests ask is the first row's.
        """
        if field in ('drives', 'media'):
            return [option['value']
                    for option in form[field]['rows'][0]['options']
                    if option['value']]
        return [option['value'] for option in form[field]['options']
                if option['value']]

    def selected(self, form, field):
        if field in ('drives', 'media'):
            return form[field]['rows'][0]['selected']
        return form[field]['selected']

    def loop_for(self, profile, **script):
        asker = prompt.Prompt(answers=answers(profile=profile, **script),
                              out=io.StringIO())
        command._ask_for_a_library(args_for(), asker=asker)
        return asker

    def test_the_same_library_models(self):
        for profile in ('IBM', 'STK', 'SONY', 'QUANTUM'):
            with self.subTest(profile=profile):
                asker = self.loop_for(profile, drives='2', tapes='20')
                self.assertEqual(asked_about(asker, 'Library model')['options'],
                                 self.offered(self.form(profile),
                                                              'library_model'))

    def test_the_same_drives_for_the_same_model(self):
        for profile in ('IBM', 'STK', 'SONY'):
            with self.subTest(profile=profile):
                form = self.form(profile)
                model = form['library_model']['selected']
                asker = self.loop_for(profile, model=model, drives='2',
                                      tapes='20')
                self.assertEqual(asked_about(asker, 'Drive model')['options'],
                                 self.offered(form, 'drives'))

    def test_the_same_densities_for_the_same_drive(self):
        """Not quite the same question, and the difference is deliberate: the
        loop offers what a drive can WRITE, because it is asking what to put
        in the library, while the form offers everything the drive LOADS and
        marks the read-only ones - a restore-only library is a real thing to
        build. So the loop's list is the writable part of the form's."""
        from apps.libraries.services.profiles import catalogue

        for profile in ('IBM', 'STK', 'QUANTUM'):
            with self.subTest(profile=profile):
                form = self.form(profile)
                drive = self.selected(form, 'drives')
                asker = self.loop_for(profile, drive=drive, drives='2',
                                      tapes='20')
                offered = asked_about(asker, 'Cartridge density')['options']
                self.assertEqual(offered, catalogue.writes(drive))
                loads = self.offered(form, 'media')
                self.assertEqual([density for density in loads
                                  if density in offered], offered)

    def test_the_same_defaults(self):
        """The loop asks apply_defaults and the form asks setup_form, which
        asks apply_defaults - so these must agree."""
        for profile in ('IBM', 'STK', 'SONY', 'HP'):
            with self.subTest(profile=profile):
                form = self.form(profile)
                asker = self.loop_for(profile)
                self.assertEqual(asked_about(asker, 'Library model')['default'],
                                 form['library_model']['selected'])
                self.assertEqual(asked_about(asker, 'Drive model')['default'],
                                 self.selected(form, 'drives'))
                self.assertEqual(
                    asked_about(asker, 'Cartridge density')['default'],
                    self.selected(form, 'media'))
                self.assertEqual(
                    asked_about(asker, 'How many cartridges')['default'],
                    form['counts']['media_count'])
                self.assertEqual(asked_about(asker, 'Empty slots')['default'],
                                 form['counts']['empty_slots'])

    def test_the_same_default_drive_once_a_density_is_chosen(self):
        """The case where they disagreed: the page took the last writer
        listed and chose a half-height HH9 for LTO9, where the service - and
        so the loop - chooses the full-height TD9."""
        from apps.libraries.services.profiles import catalogue

        form = self.form('IBM', library_model='03584L32', wanted_media='LTO9')
        self.assertEqual(self.selected(form, 'drives'),
                         catalogue.default_drive_for('IBM', '03584L32',
                                                     media='LTO9'))
        self.assertEqual(self.selected(form, 'drives'), 'ULT3580-TD9')

    def test_the_terminal_now_has_the_forms_drive_cap(self):
        """The page capped its drive count by the host's free SCSI targets and
        the loop capped by the model's layout alone, so the loop would offer
        511 drives on a host with 71 targets free. Both ask setup_form."""
        form = self.form('IBM', library_model='03584L32')
        asker = self.loop_for('IBM', model='03584L32')
        self.assertEqual(asked_about(asker, 'How many drives')['maximum'],
                         form['limits']['max_drives'])
        self.assertLess(form['limits']['max_drives'],
                        form['limits']['layout_max_drives'])
