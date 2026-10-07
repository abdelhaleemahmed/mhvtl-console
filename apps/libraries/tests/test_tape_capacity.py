"""How big a new cartridge is, and why that is one rule.

TWO QUESTIONS, NOT ONE
----------------------
Since 6 October 2026 this module guards two functions that used to be one:

    native_mb(density)   what a cartridge of this density really holds.
                         An LTO-8 is 12 TB. A fact about hardware.

    size_for(density)    how big one is made here. 1,000 MB unless the
                         settings file says otherwise. A fact about this host.

They were the same function, which is why the console made 12 TB cartridges on
a system whose purpose is testing: at the speed this host writes, filling one
takes 63 hours, so end of tape and multi-volume spanning - the things a virtual
library exists to exercise - were out of reach. The files are sparse, so the
size costs no disk; it costs time.

The chain `size_for` walks, each level narrower than the last:

    1. settings.DEFAULT_TAPE_SIZE_MB   1,000 MB, in the code
    2. tape.size.default               the settings file, every density
    3. tape.size.<density>             the settings file, one density
    4. --size-mb, or the form          one cartridge, applied by the caller

**There is no fifth level, and that is what the guard below is for.** The rule
used to be "no default anywhere"; it is now "one default, and one chain to it".

WHAT CAME BEFORE
----------------
Two numbers stood in for a cartridge's capacity until 4 October 2026, and
they disagreed:

    services/tapes/service.py          DEFAULT_SIZE_MB = 500
    mhvtl_cli/commands/tape.py         --size-mb default=500000  (x2)
    tape_operations_views.py           '500000'                  (x3)
    create_tape.html, _bulk.html       value="500000"            (x2)
    static/js/tape-media.js            UNKNOWN_SIZE_MB = 1000

So a library's own tapes were 500 MB and any tape added to it afterwards was
500 GB. `mhvtl tape list` showed them side by side - 500 for the three LTO-8
cartridges a library was created with, 500000 for the LTO-6 added next to
them - which is how this was found.

Neither number was ever a cartridge. The capacities are in
profiles/personalities.NATIVE_CAPACITY_GB, transcribed from MHVTL and checked
against it, and the create forms were already *suggesting* them through
`native_mb`. Now they are the default, and every front end leaves the number
alone.
"""
import re
import shutil
from pathlib import Path
from unittest import mock

from .base import SimpleTestCase, TestCase

from apps.libraries.services.config import settings
from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.profiles import personalities
from apps.libraries.services.tapes import TapeService, media
from apps.libraries.services.tapes.service import (UNKNOWN_SIZE_MB, native_mb,
                                                   native_size_mb, size_for)

GUI = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / 'fixtures'


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


class NativeSizeTests(SimpleTestCase):

    def test_it_is_the_density_s_native_capacity(self):
        """A real cartridge, not a round number: an LTO-8 holds 12 TB."""
        self.assertEqual(native_size_mb('LTO8'), 12_000_000)
        self.assertEqual(native_size_mb('LTO6'), 2_500_000)
        self.assertEqual(native_size_mb('LTO9'), 18_000_000)
        self.assertEqual(native_size_mb('T10KC'), 5_000_000)
        self.assertEqual(native_size_mb('AIT4'), 200_000)

    def test_every_density_mhvtl_knows_gets_its_own_capacity(self):
        for density, capacity_gb in personalities.NATIVE_CAPACITY_GB.items():
            with self.subTest(density=density):
                self.assertEqual(native_size_mb(density), capacity_gb * 1000)

    def test_a_density_with_no_native_capacity_gets_mhvtl_s_own_fallback(self):
        """MHVTL gives 9840 and 9940 one gigabyte, and so does this."""
        self.assertNotIn('9840C', personalities.NATIVE_CAPACITY_GB)
        self.assertEqual(native_size_mb('9840C'), UNKNOWN_SIZE_MB)
        self.assertEqual(UNKNOWN_SIZE_MB, 1000)

    def test_it_does_not_mind_the_case_or_a_missing_density(self):
        self.assertEqual(native_size_mb('lto8'), 12_000_000)
        self.assertEqual(native_size_mb(None), UNKNOWN_SIZE_MB)
        self.assertEqual(native_size_mb(''), UNKNOWN_SIZE_MB)

    def test_a_capacity_fits_the_forms_own_maximum(self):
        """The create forms cap the field at 50 TB; the largest cartridge
        MHVTL knows has to be offerable in them."""
        largest = max(native_size_mb(d)
                      for d in personalities.NATIVE_CAPACITY_GB)
        self.assertLessEqual(largest, 50_000_000)


class WhatGetsCreatedTests(TestCase):
    """The size that actually reaches mktape."""

    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.media = self.tmpdir()
        self.service = TapeService(self.config, self.media)

    def test_a_tape_created_with_no_size_gets_the_shipped_default(self):
        """Not the native capacity. An LTO-8 holds 12 TB and is made at 1 GB,
        because this is a library for testing and nobody fills 12 TB - 63
        hours at the speed this host writes."""
        with mock.patch.object(media, 'create', return_value=ok()) as made:
            result = self.service.create(10, 'E01099L8', slot=25)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(made.call_args[1]['density'], 'LTO8')
        self.assertEqual(made.call_args[1]['size_mb'],
                         settings.DEFAULT_TAPE_SIZE_MB)

    def test_the_settings_file_decides_when_it_says_so(self):
        """Level 3 of the chain: one density, named in the operator's file."""
        base = Path(self.tmpdir())
        settings.write(settings.put('tape.size.LTO8', 12_000_000, base), base)
        with mock.patch.object(settings, 'settings_path',
                               return_value=settings.settings_path(base)), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create(10, 'E01097L8', slot=27)
        self.assertEqual(made.call_args[1]['size_mb'], 12_000_000)

    def test_an_explicit_size_still_beats_everything(self):
        """Level 4. The forms and --size-mb are the narrowest level and win
        over the file and the code alike."""
        with mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create(10, 'E01098L8', slot=26, size_mb=4096)
        self.assertEqual(made.call_args[1]['size_mb'], 4096)

    def test_every_tape_of_a_mixed_library_is_asked_for_separately(self):
        """create_missing resolves the density per barcode, so the size has to
        follow per barcode - otherwise a settings file that sizes one
        generation and not the other would be applied to both."""
        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create_missing(10)

        by_barcode = {call[1]['density']: call[1]['size_mb']
                      for call in made.call_args_list}
        self.assertGreater(len(by_barcode), 1, 'the fixture is not mixed')
        for density, size_mb in by_barcode.items():
            with self.subTest(density=density):
                self.assertEqual(size_mb, size_for(density))


class NoFrontEndCarriesTheNumberTests(SimpleTestCase):
    """The guard. Six places held 500000 or 500; none may again."""

    #: Where a tape size could be written out again. The JS is included: it
    #: had the 1 GB fallback, and now takes it from the service through
    #: media_info.
    WATCHED = (
        'mhvtl_cli/commands/tape.py',
        'apps/libraries/tape_operations_views.py',
        'apps/libraries/templates/libraries/operator/create_tape.html',
        'apps/libraries/templates/libraries/operator/create_tapes_bulk.html',
        'static/js/tape-media.js',
        'apps/libraries/services/libraries/workflow.py',
    )

    @staticmethod
    def code_of(text: str) -> str:
        """The file with its comments taken out.

        A comment is allowed to name the old numbers - several do, because
        what they were is the reason this guard exists. Django's
        ``{% comment %}`` blocks and ``{# #}`` count, as do ``<!-- -->``,
        ``//`` and ``#``: the first version of this test failed on its own
        explanation.
        """
        text = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '',
                      text, flags=re.S)
        text = re.sub(r'\{#.*?#\}', '', text, flags=re.S)
        text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
        kept = []
        for line in text.splitlines():
            if line.lstrip().startswith(('#', '//', '*')):
                continue
            kept.append(re.sub(r'(#|//).*$', '', line))
        return '\n'.join(kept)

    def test_no_caller_writes_a_capacity_of_its_own(self):
        for name in self.WATCHED:
            code = self.code_of((GUI / name).read_text())
            for line in code.splitlines():
                with self.subTest(file=name, line=line.strip()[:60]):
                    self.assertNotRegex(line, r'\b500000\b')
                    self.assertNotRegex(line, r'size_mb\s*[=:]\s*\d')

    def test_the_capacity_table_is_the_only_place_with_capacities(self):
        """personalities.NATIVE_CAPACITY_GB, and nothing else."""
        rule = (GUI / 'apps/libraries/services/tapes/service.py').read_text()
        self.assertIn('NATIVE_CAPACITY_GB', rule)
        self.assertNotIn('DEFAULT_SIZE_MB', rule)

    def test_there_is_exactly_one_shipped_default(self):
        """The rule this file used to enforce was "no default anywhere". It is
        now "one default, in config/settings.py, and one chain to it" - which
        is a different rule and was changed deliberately, not drifted into.

        tapes/service.py must not grow its own: it asks settings for the
        number, and the only literal left there is UNKNOWN_SIZE_MB, which
        answers what an unknown density *holds* rather than what it is made at.
        """
        rule = (GUI / 'apps/libraries/services/tapes/service.py').read_text()
        self.assertIn('settings.tape_size_mb', rule)
        self.assertNotRegex(self.code_of(rule),
                            r'DEFAULT_TAPE_SIZE_MB\s*=\s*\d')

    def test_the_two_questions_have_two_functions(self):
        """`native_mb` is what the cartridge holds; `size_for` is how big one
        is made. One function answering both is the bug this release fixed."""
        from apps.libraries.services.tapes import service as rule

        self.assertEqual(rule.native_mb('LTO8'), 12_000_000)
        self.assertEqual(rule.size_for('LTO8'), settings.DEFAULT_TAPE_SIZE_MB)
        self.assertNotEqual(rule.native_mb('LTO8'), rule.size_for('LTO8'))

    def test_the_old_name_still_answers_what_the_hardware_holds(self):
        """`native_size_mb` read as "the size" and was the size. It is an
        alias for `native_mb` while the callers move, and it must not quietly
        become the created size again."""
        self.assertEqual(native_size_mb('LTO8'), native_mb('LTO8'))

    def test_the_workflow_keeps_no_tape_size(self):
        """A library's own tapes were the 500 MB ones, which is what made the
        disagreement visible."""
        workflow = (GUI
                    / 'apps/libraries/services/libraries/workflow.py').read_text()
        self.assertNotIn('DEFAULT_TAPE_SIZE_MB = ', workflow)

    def test_the_script_takes_the_fallback_from_the_service(self):
        script = (GUI / 'static/js/tape-media.js').read_text()
        self.assertIn('info.unknown_size_mb', script)
        self.assertNotRegex(
            re.sub(r'//.*', '', script), r'UNKNOWN_SIZE_MB\s*=\s*\d')

    def test_the_script_fills_the_field_from_the_service_too(self):
        """`native_mb` was what the field was pre-filled with, and the view
        derived it. Now the service says what a cartridge will be made at and
        the page renders that; the native capacity is shown beside it."""
        script = re.sub(r'//.*', '',
                        (GUI / 'static/js/tape-media.js').read_text())
        self.assertIn('info.default_mb', script)
        self.assertNotRegex(script, r'\*\s*1000')

    def test_the_view_derives_no_capacity(self):
        """It assembled the whole media context out of `personalities`, and
        one line of it - `gb * 1000` - was a decision in a view."""
        view = self.code_of(
            (GUI / 'apps/libraries/tape_operations_views.py').read_text())
        self.assertNotRegex(view, r'\*\s*1000')
        self.assertNotIn('NATIVE_CAPACITY_GB', view)


class TheFormAgreesWithTheServiceTests(TestCase):
    """The guard the original bug never had.

    500 and 500000 could disagree because nothing compared them. The number a
    creation form is pre-filled with and the number a create with no size
    produces are now asserted to be the same, for every density there is.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.service = TapeService(self.config, self.tmpdir())

    def test_what_the_form_offers_is_what_a_create_would_produce(self):
        offered = self.service.media_context([])['media_info']['default_mb']
        self.assertTrue(offered, 'the form was offered no sizes at all')
        for density, mb in offered.items():
            with self.subTest(density=density):
                self.assertEqual(mb, size_for(density))

    def test_the_form_is_told_what_the_cartridge_really_holds_as_well(self):
        info = self.service.media_context([])['media_info']
        self.assertEqual(info['native_mb']['LTO8'], 12_000_000)
        self.assertEqual(info['default_mb']['LTO8'],
                         settings.DEFAULT_TAPE_SIZE_MB)

    def test_both_maps_cover_every_density_that_can_be_created(self):
        info = self.service.media_context([])['media_info']
        for density in personalities.SUFFIX_BY_DENSITY:
            with self.subTest(density=density):
                self.assertIn(density, info['default_mb'])
                self.assertIn(density, info['native_mb'])


class BothCreationPathsTakeASizeTests(TestCase):
    """`library create` could not size its own cartridges.

    `tape create` and `tape bulk` have had --size-mb from the beginning; a
    library's own tapes were whatever the default was, with no way to say
    otherwise at the moment they were made. That was the last place the two
    creation paths disagreed about a capacity - the thing 3.2.0 set out to fix
    and did not finish.

    The specification already carried it: libraries/workflow reads
    spec['tape_size_mb']. What was missing was a flag and a field.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)

    def test_the_command_line_takes_one(self):
        from mhvtl_cli.main import build_parser

        args = build_parser().parse_args(
            ['library', 'create', '--profile', 'IBM', '--size-mb', '5000'])
        self.assertEqual(args.tape_size_mb, 5000)

    def test_a_library_spec_carrying_one_reaches_the_media(self):
        """The whole point: the number typed on either front end is the
        number mktape is asked for."""
        from apps.libraries.services.libraries import workflow

        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            workflow._create_media(
                10, {'tape_size_mb': 4096, 'media_type': 'LTO8'}, self.config)

        self.assertTrue(made.call_args_list, 'no media was created')
        for call in made.call_args_list:
            self.assertEqual(call[1]['size_mb'], 4096)

    def test_without_one_the_chain_decides(self):
        from apps.libraries.services.libraries import workflow

        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            workflow._create_media(10, {'media_type': 'LTO8'}, self.config)

        self.assertTrue(made.call_args_list, 'no media was created')
        for call in made.call_args_list:
            self.assertEqual(call[1]['size_mb'], size_for(call[1]['density']))

    def test_the_form_offers_the_size_a_create_would_use(self):
        """The field's placeholder is the service's answer, not the page's."""
        from apps.libraries.services.libraries import setup_form

        answer = setup_form.state('IBM')
        self.assertTrue(answer.success, answer.errors)
        shown = answer.data['tape_size']
        self.assertEqual(shown['mb'], size_for(shown['density'] or ''))


class OneSizePerKindTests(TestCase):
    """A library can hold more than one kind of cartridge.

    One size for the library would give a DLT-4 the LTO-8's capacity. The size
    belongs to the kind, which means it travels on the media run - the setup
    form's per-row field, `--media-size`, and a preset's `size_mb`.
    """

    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.service = TapeService(self.config, self.tmpdir())

    def test_each_kind_gets_the_size_asked_for_it(self):
        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create_missing(
                10, sizes={'LTO8': 12_000_000, 'LTO6': 2_500_000})

        by_density = {call[1]['density']: call[1]['size_mb']
                      for call in made.call_args_list}
        self.assertGreater(len(by_density), 1, 'the fixture is not mixed')
        self.assertEqual(by_density.get('LTO8'), 12_000_000)
        self.assertEqual(by_density.get('LTO6'), 2_500_000)

    def test_a_kind_not_named_falls_through_to_the_chain(self):
        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create_missing(10, sizes={'LTO8': 12_000_000})

        for call in made.call_args_list:
            density = call[1]['density']
            with self.subTest(density=density):
                expected = 12_000_000 if density == 'LTO8' else size_for(density)
                self.assertEqual(call[1]['size_mb'], expected)

    def test_one_size_for_everything_still_wins(self):
        """`tape bulk --size-mb` means every cartridge in the run."""
        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create_missing(10, size_mb=4096,
                                        sizes={'LTO8': 12_000_000})

        for call in made.call_args_list:
            self.assertEqual(call[1]['size_mb'], 4096)

    def test_the_command_line_takes_a_size_per_kind(self):
        from mhvtl_cli.main import build_parser

        args = build_parser().parse_args(
            ['library', 'create', '--profile', 'IBM',
             '--media-size', 'LTO8:12TB', '--media-size', 'DLT4:20GB'])
        self.assertEqual(args.media_sizes, ['LTO8:12TB', 'DLT4:20GB'])

    def test_a_media_run_carries_its_size_through_a_preset(self):
        """`--save-preset` must not quietly lose them: a value accepted,
        stored and then ignored is the trap presets.py already documents."""
        from apps.libraries.services.config import presets
        from apps.libraries.services.libraries import spec as libspec

        kept = presets.savable({
            'profile': 'IBM', 'library_model': '03584L32',
            'drive': [{'model': 'ULT3580-TDA', 'count': 2}],
            'media': [{'density': 'LTO10', 'count': 4},
                      {'density': 'LTO9', 'count': 2}],
            'tape_sizes': {'LTO10': 30_000_000, 'LTO9': 2_000_000}})
        self.assertEqual([entry.get('size_mb') for entry in kept['media']],
                         [30_000_000, 2_000_000])

        back = presets.parse(presets.render({'m': kept}),
                             profile_names=['IBM'])['m']
        back['library_id'] = 90
        self.assertEqual(libspec.apply_defaults(back)['tape_sizes'],
                         {'LTO10': 30_000_000, 'LTO9': 2_000_000})

    def test_a_preset_size_that_is_not_a_size_is_refused_when_it_is_read(self):
        """A preset is read far from where it was written, so a hand-edited
        zero must not reach mktape."""
        from apps.libraries.services.config import presets

        text = ('[m]\nprofile = "IBM"\n\n[[m.media]]\n'
                'density = "LTO8"\ncount = 2\nsize_mb = 0\n')
        with self.assertRaises(presets.PresetError) as refused:
            presets.parse(text, profile_names=['IBM'])
        self.assertIn('size_mb', str(refused.exception))

    def test_the_form_offers_a_size_on_every_kind_row(self):
        from apps.libraries.services.libraries import setup_form

        rows = setup_form.state('IBM').data['media']['rows']
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(density=row['selected']):
                self.assertEqual(row['size_mb'], size_for(row['selected']))
                self.assertTrue(row['size_shown'])


class DensitiesAreGroupedTests(SimpleTestCase):
    """Thirty-two generations in one flat list is a wall to read."""

    def test_every_density_lands_in_a_family(self):
        for density in personalities.SUFFIX_BY_DENSITY:
            with self.subTest(density=density):
                self.assertTrue(personalities.media_family(density))

    def test_the_families_are_what_an_operator_would_call_them(self):
        self.assertEqual(personalities.media_family('LTO8'), 'LTO')
        self.assertEqual(personalities.media_family('AIT4'), 'AIT')
        self.assertEqual(personalities.media_family('DLT4'), 'DLT')
        self.assertEqual(personalities.media_family('T10KC'), 'T10000')
        self.assertEqual(personalities.media_family('E07'), '3592')

    def test_sdlt_is_not_read_as_dlt(self):
        """Longest prefix first, or every SDLT cartridge joins the DLT group
        and the two are different tapes."""
        self.assertEqual(personalities.media_family('SDLT600'), 'SDLT')
        self.assertNotEqual(personalities.media_family('SDLT600'),
                            personalities.media_family('DLT4'))

    def test_the_form_is_given_them_grouped(self):
        service = TapeService(str(FIXTURES))
        families = dict(service.media_context([])['density_families'])
        self.assertIn('LTO', families)
        self.assertIn('AIT', families)
        self.assertEqual([d for d, _ in families['AIT']],
                         ['AIT1', 'AIT2', 'AIT3', 'AIT4'])


class TheOptionSaysWhatYouWillGetTests(SimpleTestCase):
    """What the drop-downs are labelled with.

    They read ``LTO8 (12 TB)`` until 7 October 2026, built from
    ``NATIVE_CAPACITY_GB`` by a ``personalities.media_label`` that no longer
    exists. That was right while a cartridge was created at its native
    capacity and wrong the moment the size became a setting: the option named
    12 TB and the create produced 1 GB, in the one place an operator is
    choosing, out by a factor of twelve thousand.

    So the label is the *configured* size now - the answer ``size_for``
    gives, which is the answer the create gives. The native capacity has not
    gone anywhere; it is a column of its own on the Settings page and in
    ``mhvtl settings list``, and the help line under the size field names it
    as the figure to type for a full-size tape.
    """

    def setUp(self):
        self.base = Path(self.tmpdir())

    def label(self, density):
        from apps.libraries.services.tapes.service import media_label
        return media_label(density, self.base)

    # -- it is the created size, not the capacity --------------------------

    def test_with_no_settings_file_it_is_the_shipped_default(self):
        self.assertEqual(self.label('LTO8'), 'LTO8 (1 GB)')

    def test_it_is_never_what_the_hardware_holds_by_accident(self):
        """The assertion the old label would have failed."""
        for density in ('LTO8', 'LTO9', 'LTO10', 'E07', 'AIT4'):
            with self.subTest(density=density):
                self.assertNotIn(settings.as_short_size(native_mb(density)),
                                 self.label(density))

    def test_setting_one_density_changes_only_that_option(self):
        settings.write(settings.put('tape.size.LTO8', 12_000_000, self.base),
                       self.base)
        self.assertEqual(self.label('LTO8'), 'LTO8 (12 TB)')
        self.assertEqual(self.label('LTO9'), 'LTO9 (1 GB)')

    def test_the_file_default_moves_every_option(self):
        settings.write(settings.put('tape.size.default', 2000, self.base),
                       self.base)
        self.assertEqual(self.label('LTO8'), 'LTO8 (2 GB)')
        self.assertEqual(self.label('DLT4'), 'DLT4 (2 GB)')

    def test_the_label_and_the_create_cannot_disagree(self):
        """The guard the original bug never had, applied to the label: it is
        derived from `size_for` and not from a table beside it."""
        settings.write(settings.put('tape.size.LTO7', 6_000_000, self.base),
                       self.base)
        for density in personalities.SUFFIX_BY_DENSITY:
            with self.subTest(density=density):
                shown = settings.as_short_size(
                    size_for(density, self.base))
                self.assertEqual(self.label(density), f'{density} ({shown})')

    # -- the short form ----------------------------------------------------

    def test_an_option_carries_one_number_and_not_two(self):
        """`as_size` gives "12 TB (12,000,000 MB)", which in an option is two
        sets of brackets and a figure nobody copies out of a drop-down."""
        self.assertEqual(settings.as_short_size(12_000_000), '12 TB')
        self.assertEqual(settings.as_size(12_000_000),
                         '12 TB (12,000,000 MB)')

    def test_a_size_that_does_not_divide_stays_in_mb(self):
        """1,048 MB is not "1 GB": an operator comparing it with the 1,000 MB
        they typed would be given no way to see that the two differ."""
        self.assertEqual(settings.as_short_size(1048), '1,048 MB')
        self.assertEqual(settings.as_short_size(2500), '2.5 GB')

    def test_a_density_with_no_name_gets_no_label(self):
        self.assertEqual(self.label(''), '')
        self.assertEqual(self.label(None), '')

    def test_it_does_not_mind_the_case(self):
        self.assertEqual(self.label('lto8'), 'LTO8 (1 GB)')

    # -- and nothing builds its own ----------------------------------------

    def test_only_the_tapes_service_builds_one(self):
        """`\\bdef media_label\\b` does not match `def _media_label`, which is
        setup_form's own row label - the density and its barcode suffix, with
        no size in it at all."""
        for name in ('apps/libraries/services/profiles/personalities.py',
                     'apps/libraries/services/libraries/setup_form.py',
                     'apps/libraries/views.py',
                     'apps/libraries/tape_operations_views.py'):
            with self.subTest(file=name):
                code = NoFrontEndCarriesTheNumberTests.code_of(
                    (GUI / name).read_text())
                self.assertNotRegex(code, r'\bdef media_label\b')
        self.assertRegex(
            (GUI / 'apps/libraries/services/tapes/service.py').read_text(),
            r'\bdef media_label\b')

    def test_every_chooser_takes_its_labels_from_the_service(self):
        """Three drop-downs asked for these: the tape form's density, the
        library wizard's "start from the tape", and the brand page's filter.
        """
        service = TapeService(str(FIXTURES))
        context = service.media_context([])
        labels = context['media_info']['labels']
        self.assertTrue(labels)
        for density, label in labels.items():
            with self.subTest(density=density):
                self.assertEqual(label, f'{density} '
                                        f'({settings.as_short_size(size_for(density))})')
        self.assertEqual(dict(context['densities']), labels)
