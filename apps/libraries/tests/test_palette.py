"""The generation palette: one table, ten generations, and no bucketing.

The rule this replaces lived in three places - barcodes.GENERATION_CLASS and
two JavaScript functions in the mount page - and the three disagreed. These
tests are what stops it happening again: every claim is checked against the
compatibility tables rather than against a second copy of the answer.
"""
import io
import json
import os
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.libraries.services.tapes import barcodes, compatibility, palette
from apps.libraries.services.tapes.models import TapeInfo
from mhvtl_cli import colour, main, output


def run(argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class PaletteTableTests(SimpleTestCase):
    def test_every_generation_has_a_token_of_its_own(self):
        tokens = [entry[0] for entry in palette.PALETTE.values()]
        self.assertEqual(len(tokens), len(set(tokens)),
                         'two generations share a token')
        self.assertEqual(len(palette.PALETTE), 10)

    def test_lto_10_is_not_unknown(self):
        """The table this replaces had no entry for 10, so a real LTO-10
        cartridge was drawn as if its generation could not be read."""
        self.assertEqual(palette.token_for_generation('LTO-10'), 'lto-10')
        self.assertEqual(palette.token_for_tape('K00001LA'), 'lto-10')

    def test_lto_4_does_not_borrow_lto_5s_colour(self):
        self.assertEqual(palette.token_for_generation('LTO-4'), 'lto-4')

    def test_every_generation_the_barcode_table_names_has_a_colour(self):
        for suffix, generation in compatibility.BARCODE_SUFFIX_TO_LTO.items():
            self.assertNotEqual(
                palette.token_for_generation(generation), palette.UNKNOWN,
                f'suffix {suffix} names {generation}, which has no colour')

    def test_every_drive_model_the_table_names_has_a_colour(self):
        for model in compatibility.DRIVE_MODEL_TO_LTO:
            self.assertNotEqual(palette.token_for_drive(model), palette.UNKNOWN,
                                f'{model} has no colour when empty')

    def test_rows_are_in_generation_order_with_ten_after_nine(self):
        tokens = [row['token'] for row in palette.rows(include_unknown=False)]
        self.assertEqual(tokens[-2:], ['lto-9', 'lto-10'])


class CartridgeTests(SimpleTestCase):
    def test_a_cartridge_is_its_own_generation(self):
        self.assertEqual(palette.token_for_tape('E01001L8'), 'lto-8')
        self.assertEqual(palette.token_for_tape('F01030L6'), 'lto-6')
        self.assertEqual(palette.token_for_tape('I60001L7'), 'lto-7')

    def test_media_that_is_not_lto_gets_no_colour_rather_than_a_wrong_one(self):
        """A 3592 or a T10000 is not an LTO generation drawn badly."""
        for barcode in ('JA1234JA', 'TK0001T1', ''):
            self.assertEqual(palette.token_for_tape(barcode), palette.UNKNOWN)

    def test_a_density_works_as_well_as_a_barcode(self):
        for density in ('LTO8', 'LTO-8', 'lto8'):
            self.assertEqual(palette.token_for_tape(density=density), 'lto-8')


class DriveTests(SimpleTestCase):
    def test_an_empty_drive_is_the_highest_generation_it_supports(self):
        for model, own in compatibility.DRIVE_MODEL_TO_LTO.items():
            supported = compatibility.LTO_COMPATIBILITY[own]['read']
            highest = max(supported, key=lambda g: int(g.split('-')[1]))
            self.assertEqual(palette.highest_supported(model), highest, model)
            self.assertEqual(palette.token_for_drive(model),
                             palette.token_for_generation(highest), model)

    def test_a_loaded_drive_takes_its_cartridge_not_its_own_generation(self):
        """An LTO-6 cartridge in an LTO-8 drive makes the drive LTO-6
        coloured: the thing an operator is looking for is the tape."""
        self.assertEqual(palette.token_for_drive('ULT3580-TD8', 'F01030L6'),
                         'lto-6')
        self.assertNotEqual(palette.token_for_drive('ULT3580-TD8', 'F01030L6'),
                            palette.token_for_drive('ULT3580-TD8'))

    def test_a_non_lto_drive_gets_no_colour(self):
        self.assertIsNone(palette.highest_supported('T10000B'))
        self.assertEqual(palette.token_for_drive('T10000B'), palette.UNKNOWN)


class LtfsMarkTests(SimpleTestCase):
    def test_a_cartridge_that_was_never_read_carries_no_mark(self):
        """None is NOT_ASKED. Reporting it as 'not LTFS' would be a claim the
        service never made - the cartridge may be a volume written elsewhere."""
        self.assertEqual(palette.mark_for_cartridge(None, None), '')

    def test_a_plain_cartridge_carries_no_mark_either(self):
        self.assertEqual(palette.mark_for_cartridge(False, False), '')

    def test_an_ltfs_volume_and_a_wiped_one_are_different_marks(self):
        self.assertEqual(palette.mark_for_cartridge(True, False), 'ltfs')
        self.assertEqual(palette.mark_for_cartridge(False, True), 'ltfs-was')


class CollisionTests(SimpleTestCase):
    def test_the_real_shell_colours_collide_and_the_table_says_so(self):
        groups = {group['hue']: [g['generation'] for g in group['generations']]
                  for group in palette.collisions()}
        self.assertEqual(groups['Dark red'], ['LTO-6', 'LTO-8'])
        self.assertEqual(groups['Green (dark)'], ['LTO-5', 'LTO-9'])
        self.assertEqual(groups['Black'],
                         ['LTO-1', 'LTO-2', 'LTO-7', 'LTO-10'])

    def test_every_colliding_generation_is_still_separated(self):
        for group in palette.collisions():
            ansi = [palette.PALETTE[g['generation']][2]
                    for g in group['generations']]
            self.assertEqual(len(ansi), len(set(ansi)),
                             f'{group["hue"]} is drawn the same twice')


class OneCopyTests(SimpleTestCase):
    def test_barcodes_does_not_keep_a_second_table(self):
        """The duplicate is what this whole module exists to remove."""
        self.assertFalse(hasattr(barcodes, 'GENERATION_CLASS'))

    def test_barcodes_keeps_no_generation_function_either(self):
        """It delegated for one commit. A delegate is the same two names for
        one rule, which is how the three copies started."""
        self.assertFalse(hasattr(barcodes, 'generation_class'))

    def test_the_tape_model_carries_the_token_and_not_a_class(self):
        tape = TapeInfo(barcode='E01001L8', library_id=10, slot=1)
        self.assertEqual(tape.generation_token, 'lto-8')
        self.assertFalse(hasattr(tape, 'density_class'))
        self.assertEqual(tape.to_dict()['generation_token'], 'lto-8')
        self.assertNotIn('density_class', tape.to_dict())


class PaletteCommandTests(TestCase):
    """`mhvtl op palette` - the legend, and the only place ANSI is produced."""

    def test_it_prints_every_generation_and_the_collisions(self):
        code, out, err = run(['op', 'palette'])
        self.assertEqual(code, output.EXIT_OK)
        for token in ('lto-1', 'lto-8', 'lto-10', 'lto-unknown'):
            self.assertIn(token, out)
        self.assertIn('LTO-6, LTO-8', out)
        self.assertIn('Dark red', out)

    def test_the_generation_is_always_text_so_colour_is_never_the_only_cue(self):
        code, out, err = run(['op', 'palette', '--no-colour'])
        self.assertEqual(code, output.EXIT_OK)
        for generation in ('LTO-5', 'LTO-6', 'LTO-7', 'LTO-8', 'LTO-9'):
            self.assertIn(generation, out)

    def test_no_escape_sequence_reaches_a_pipe(self):
        """redirect_stdout gives a StringIO, which is not a terminal - the
        same condition as `mhvtl op palette | less`."""
        code, out, err = run(['op', 'palette'])
        self.assertNotIn('\033[', out)

    def test_no_colour_is_honoured_even_on_a_terminal(self):
        with mock.patch('mhvtl_cli.colour._depth', return_value=256):
            stream = mock.Mock()
            stream.isatty.return_value = True
            self.assertTrue(colour.enabled(False, stream))
            self.assertFalse(colour.enabled(True, stream))

    def test_sixteen_colours_gets_none_rather_than_two_generations_alike(self):
        """The palette separates LTO-6 from LTO-8 by lightness inside one hue,
        which sixteen colours cannot hold. Two generations sharing a colour is
        worse than no colour, so the colour is dropped entirely."""
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            self.assertEqual(colour._depth(), 8)
            self.assertFalse(colour.enabled(False, stream))
        with mock.patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=True):
            self.assertTrue(colour.enabled(False, stream))

    def test_no_color_the_environment_variable_is_honoured(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch.dict(os.environ,
                             {'TERM': 'xterm-256color', 'NO_COLOR': '1'},
                             clear=True):
            self.assertFalse(colour.enabled(False, stream))

    def test_json_carries_the_palette_and_the_collisions(self):
        code, out, err = run(['--json', 'op', 'palette'])
        self.assertEqual(code, output.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(len(payload['data']['palette']), 11)
        self.assertEqual(len(payload['data']['collisions']), 3)


class ServicesEmitTheTokenTests(TestCase):
    """Step 2: the shapes the services hand to a caller carry the token.

    A front end must never work the colour out for itself - that is how the
    mount page came to hold two copies of the rule that disagreed with each
    other. So every slot and every drive leaves the service already knowing
    which generation it is drawn as.
    """

    DRIVES = [{'drive_num': 0, 'vendor': 'IBM', 'model': 'ULT3580-TD8',
               'revision': 'HB82', 'lto_generation': 'LTO-8'},
              {'drive_num': 1, 'vendor': 'IBM', 'model': 'ULT3580-TD6',
               'revision': 'G9Q1', 'lto_generation': 'LTO-6'}]

    SLOTS = [{'slot_num': 1, 'barcode': 'E01001L8', 'full': True},
             {'slot_num': 2, 'barcode': 'F01030L6', 'full': True},
             {'slot_num': 3, 'barcode': '', 'full': False}]

    def status(self, *, with_ltfs=False, loaded=None, states=None):
        """mount_status against a faked robot. `loaded` puts a cartridge in
        drive 0, which is the only way to exercise rule 2."""
        from apps.libraries.services.operations import mounting

        element = lambda **kw: mock.Mock(**kw)      # noqa: E731
        state = mock.Mock(
            drives=[element(number=d['drive_num'],
                            barcode=loaded if d['drive_num'] == 0 else None,
                            full=bool(loaded) and d['drive_num'] == 0,
                            slot_origin=None) for d in self.DRIVES],
            slots=[element(number=s['slot_num'], barcode=s['barcode'],
                           full=s['full']) for s in self.SLOTS],
            import_export=[], map_slots=[])

        with mock.patch.object(mounting.mapping, 'device_for_library',
                               return_value='/dev/sg11'), \
             mock.patch.object(mounting.mtx, 'status', return_value=state), \
             mock.patch.object(mounting, 'library_drives',
                               return_value=list(self.DRIVES)), \
             mock.patch('apps.libraries.services.tapes.media.usage_for_all',
                        return_value={}), \
             mock.patch('apps.libraries.services.tapes.ltfs_state.state_for_all',
                        return_value=states or {}):
            return mounting.mount_status(10, with_ltfs=with_ltfs)

    def test_every_slot_carries_a_token(self):
        slots = self.status().data['storage_slots']
        self.assertEqual([s['generation_token'] for s in slots],
                         ['lto-8', 'lto-6', ''])

    def test_an_empty_slot_gets_no_token_rather_than_unknown(self):
        """'lto-unknown' means read, and not an LTO generation - a 3592. An
        empty slot has no cartridge to have a generation at all."""
        empty = self.status().data['storage_slots'][2]
        self.assertEqual(empty['generation_token'], '')
        self.assertNotEqual(empty['generation_token'], palette.UNKNOWN)

    def test_an_empty_drive_is_what_it_is_for(self):
        drives = self.status().data['drives']
        self.assertEqual([d['generation_token'] for d in drives],
                         ['lto-8', 'lto-6'])

    def test_a_loaded_drive_is_drawn_as_its_cartridge(self):
        """Rule 2 beats rule 3: an LTO-6 cartridge in the LTO-8 drive makes
        that drive LTO-6 coloured."""
        drives = self.status(loaded='F01030L6').data['drives']
        self.assertEqual(drives[0]['generation_token'], 'lto-6')
        self.assertEqual(drives[1]['generation_token'], 'lto-6')
        self.assertEqual(drives[0]['model'], 'ULT3580-TD8')

    def test_library_drives_answers_from_the_configuration_alone(self):
        """No robot, no cartridge: the token is the empty-drive answer."""
        from apps.libraries.services.operations import mounting

        conf = mock.Mock()
        conf.drives_of.return_value = {
            11: {'product': 'ULT3580-TD8', 'vendor': 'IBM', 'slot': '1'},
            12: {'product': 'T10000B', 'vendor': 'STK', 'slot': '2'},
        }
        service = mock.Mock()
        service.device_conf.return_value = conf
        with mock.patch.object(mounting, 'ConfigService', return_value=service):
            drives = mounting.library_drives(10)
        self.assertEqual([d['generation_token'] for d in drives],
                         ['lto-8', palette.UNKNOWN])

    def test_no_ltfs_mark_until_a_cartridge_is_read(self):
        for slot in self.status(with_ltfs=False).data['storage_slots']:
            self.assertNotIn('ltfs_mark', slot)

    def test_a_cartridge_that_was_skipped_carries_an_empty_mark(self):
        """Asked for, but the gate answered or the read found nothing: the
        mark is empty, which is not the same as "not an LTFS volume"."""
        for slot in self.status(with_ltfs=True).data['storage_slots']:
            self.assertEqual(slot['ltfs_mark'], '')

    def test_a_volume_and_a_wiped_one_get_their_marks(self):
        states = {'E01001L8': mock.Mock(state='ltfs', was_ltfs=False,
                                        summary='an LTFS volume'),
                  'F01030L6': mock.Mock(state='plain', was_ltfs=True,
                                        summary='was an LTFS volume')}
        slots = self.status(with_ltfs=True, states=states).data['storage_slots']
        self.assertEqual([s['ltfs_mark'] for s in slots],
                         ['ltfs', 'ltfs-was', ''])

    def test_the_mark_never_replaces_the_generation(self):
        """LTFS is a mark, never a fill: a volume keeps its own colour."""
        states = {'E01001L8': mock.Mock(state='ltfs', was_ltfs=False,
                                        summary='an LTFS volume')}
        volume = self.status(with_ltfs=True, states=states).data['storage_slots'][0]
        self.assertEqual(volume['generation_token'], 'lto-8')
        self.assertEqual(volume['ltfs_mark'], 'ltfs')


class TapeListTokenTests(SimpleTestCase):
    """The tape listing hands out the same two values, by the same names."""

    def test_a_tape_carries_the_token_and_the_mark(self):
        tape = TapeInfo(barcode='I60001L7', library_id=60, slot=1,
                        ltfs_state='ltfs')
        row = tape.to_dict()
        self.assertEqual(row['generation_token'], 'lto-7')
        self.assertEqual(row['ltfs_mark'], 'ltfs')

    def test_a_tape_nobody_read_carries_no_mark(self):
        row = TapeInfo(barcode='I60001L7', library_id=60, slot=1).to_dict()
        self.assertEqual(row['ltfs_mark'], '')
        self.assertFalse(row['ltfs'], 'ltfs is a bool and cannot say NOT_ASKED')

    def test_a_wiped_volume_is_its_own_mark(self):
        row = TapeInfo(barcode='I60004L7', library_id=60, slot=4,
                       ltfs_state='plain', ltfs_was=True).to_dict()
        self.assertEqual(row['ltfs_mark'], 'ltfs-was')


class LayoutCommandTests(TestCase):
    """`mhvtl op layout` - the map drawn in a terminal.

    The command chooses nothing but where things sit on the screen. Which
    generation a cartridge is, which a drive is drawn as and whether a
    cartridge is an LTFS volume were all decided in the services, so these
    tests are about the drawing: alignment, what survives --no-colour, and
    what a gated library is told.
    """

    def status(self, *, slots=None, drives=None, ltfs=False):
        from apps.libraries.services.core import success_result

        drives = drives if drives is not None else [
            {'drive_num': 0, 'model': 'ULT3580-TD8', 'full': False,
             'barcode': None, 'lto_generation': 'LTO-8',
             'generation_token': 'lto-8', 'tape_lto': None},
            {'drive_num': 1, 'model': 'ULT3580-TD6', 'full': True,
             'barcode': 'F01030L6', 'lto_generation': 'LTO-6',
             'generation_token': 'lto-6', 'tape_lto': 'LTO-6'},
        ]
        slots = slots if slots is not None else [
            {'slot_num': 1, 'barcode': 'E01001L8', 'full': True,
             'generation_token': 'lto-8', 'tape_lto': 'LTO-8'},
            {'slot_num': 2, 'barcode': '', 'full': False,
             'generation_token': ''},
            {'slot_num': 3, 'barcode': 'F01031L6', 'full': True,
             'generation_token': 'lto-6', 'tape_lto': 'LTO-6'},
        ]
        if ltfs:
            for slot, mark in zip(slots, ['ltfs', '', 'ltfs-was']):
                slot['ltfs_mark'] = mark
                slot['ltfs'] = mark == 'ltfs'
        return success_result('drawn', {
            'library_id': 10, 'device_path': '/dev/sg4',
            'drives': drives, 'storage_slots': slots,
            'generations_present': palette.present_in(
                [d['generation_token'] for d in drives]
                + [s['generation_token'] for s in slots]),
            'import_export_slots': [{'slot_num': 1, 'barcode': '',
                                     'full': False}],
            'slot_summary': {'total_slots': len(slots),
                             'full_slots': sum(1 for s in slots if s['full'])},
        })

    def draw(self, argv, **kw):
        from apps.libraries.services.operations import mounting
        with mock.patch.object(mounting, 'mount_status',
                               return_value=self.status(**kw)):
            return run(argv)

    def test_it_draws_every_slot_and_every_drive(self):
        code, out, err = self.draw(['op', 'layout', '10', '--no-colour'])
        self.assertEqual(code, output.EXIT_OK)
        for barcode in ('E01001L8', 'F01031L6', 'F01030L6'):
            self.assertIn(barcode, out)
        self.assertIn('Drive 0', out)
        self.assertIn('/dev/sg4', out)

    def test_a_barcode_is_never_shortened_to_make_a_row_fit(self):
        """A barcode cut to fit a terminal cannot be copied, and the reader
        has no way to know it was cut. Fewer columns is what gives."""
        code, out, err = self.draw(['op', 'layout', '10', '--no-colour',
                                    '--width', '1'])
        self.assertIn('E01001L8', out)
        rows = [l for l in out.splitlines() if 'E01001L8' in l]
        self.assertEqual(len(rows), 1)

    def test_an_empty_slot_takes_the_same_room_as_a_full_one(self):
        """A grid that drifts by a character wherever a slot is empty is not
        a grid. The columns are checked by position, not by eye."""
        slots = [{'slot_num': n, 'barcode': f'E0100{n}L8' if n != 2 else '',
                  'full': n != 2, 'generation_token': 'lto-8' if n != 2 else '',
                  'tape_lto': 'LTO-8'} for n in range(1, 7)]
        code, out, err = self.draw(['op', 'layout', '10', '--no-colour',
                                    '--width', '3'], slots=slots)
        rows = [l for l in out.splitlines() if 'E0100' in l]
        self.assertEqual(len(rows), 2)
        self.assertIn('\u00b7', rows[0], 'the empty slot is drawn, not skipped')
        # Slot 4 opens the second row, so it must start where slot 1 does, and
        # slot 6 must start where slot 3 does - across the empty cell.
        for first, second in (('1', '4'), ('3', '6')):
            self.assertEqual(rows[0].index(f' {first} '),
                             rows[1].index(f' {second} '),
                             f'column drifted between slot {first} and {second}')

    def test_the_generation_survives_no_colour(self):
        """Colour is a hint, never the identifier."""
        code, out, err = self.draw(['op', 'layout', '10', '--no-colour'])
        self.assertIn('LTO-8', out)
        self.assertIn('LTO-6', out)
        self.assertNotIn('\033[', out)

    def test_the_legend_lists_only_what_this_library_holds(self):
        code, out, err = self.draw(['op', 'layout', '10', '--no-colour'])
        legend = next(l for l in out.splitlines() if l.startswith('Legend'))
        self.assertIn('LTO-6', legend)
        self.assertIn('LTO-8', legend)
        self.assertNotIn('LTO-9', legend)
        self.assertNotIn('LTO-1 ', legend)

    def test_a_loaded_drive_is_drawn_as_its_cartridge(self):
        code, out, err = self.draw(['op', 'layout', '10', '--no-colour'])
        loaded = next(l for l in out.splitlines() if 'Drive 1' in l)
        self.assertIn('[LTO-6]', loaded)
        self.assertIn('F01030L6', loaded)

    def test_the_ltfs_marks_appear_only_when_asked_for(self):
        code, plain, err = self.draw(['op', 'layout', '10', '--no-colour'])
        self.assertNotIn('an LTFS volume', plain)
        code, marked, err = self.draw(['op', 'layout', '10', '--ltfs',
                                       '--no-colour'], ltfs=True)
        self.assertIn('an LTFS volume', marked)
        self.assertIn('was LTFS', marked)

    def test_a_gated_library_is_told_so_rather_than_shown_blanks(self):
        """Gate 1: no drive here can open LTFS, so nothing was read. `ltfs` is
        None on every slot, which is not False."""
        slots = [{'slot_num': 1, 'barcode': 'G03001TA', 'full': True,
                  'generation_token': 'lto-unknown', 'tape_lto': None,
                  'ltfs': None, 'ltfs_mark': ''}]
        code, out, err = self.draw(['op', 'layout', '30', '--ltfs',
                                    '--no-colour'], slots=slots)
        self.assertIn('No cartridge was read', out)
        self.assertNotIn('an LTFS volume', out)

    def test_media_with_no_lto_generation_says_so(self):
        """'not LTO' rather than 'LTO-unknown': a T10000 is not a generation
        called unknown, it has no LTO generation at all."""
        drives = [{'drive_num': 0, 'model': 'T10000B', 'full': False,
                   'barcode': None, 'lto_generation': 'Unknown',
                   'generation_token': 'lto-unknown', 'tape_lto': None}]
        code, out, err = self.draw(['op', 'layout', '30', '--no-colour'],
                                   drives=drives)
        self.assertIn('not an LTO drive', out)
        self.assertIn('not LTO', out)
        self.assertNotIn('LTO-unknown', out)

    def test_json_hands_back_the_service_payload_untouched(self):
        code, out, err = self.draw(['--json', 'op', 'layout', '10'])
        payload = json.loads(out)
        self.assertEqual(payload['data']['library_id'], 10)
        self.assertEqual(payload['data']['storage_slots'][0]['generation_token'],
                         'lto-8')


class LtfsOutlineTests(TestCase):
    """LTFS gets an outline round the tile, the way the web page draws it.

    A terminal has no box-shadow, so the outline is a bar down each side. It
    is still a MARK and never a fill: the cartridge keeps its generation's
    colour, and the outline is added to it.
    """

    def test_a_volume_and_a_wiped_one_are_different_colours(self):
        volume = colour.ltfs_outline('ltfs')
        was = colour.ltfs_outline('ltfs-was')
        self.assertIn(str(colour.LTFS_ACCENT), volume[0])
        self.assertIn(str(colour.LTFS_MUTED), was[0])
        self.assertNotEqual(volume, was)

    def test_a_cartridge_with_no_mark_gets_two_spaces(self):
        """Not read, or read and plain: either way nothing is drawn, and the
        cell still occupies the same room."""
        self.assertEqual(colour.ltfs_outline(''), (' ', ' '))

    def test_every_outline_is_one_column_wide(self):
        import re
        plain = re.compile(r'\033\[[0-9;]*m')
        for mark in ('', 'ltfs', 'ltfs-was'):
            for on in (True, False):
                left, right = colour.ltfs_outline(mark, on=on)
                self.assertEqual(len(plain.sub('', left)), 1, (mark, on))
                self.assertEqual(len(plain.sub('', right)), 1, (mark, on))

    def test_without_colour_the_two_states_stay_distinguishable(self):
        """Two identical bars in no colour would make a volume look exactly
        like one that merely was - the distinction the LTFS state model
        exists to keep. So the plain fallback is two different characters."""
        volume = colour.ltfs_outline('ltfs', on=False)
        was = colour.ltfs_outline('ltfs-was', on=False)
        self.assertNotEqual(volume, was)
        self.assertEqual(volume[1], '*')
        self.assertEqual(was[1], '~')

    def test_the_outline_never_replaces_the_generation_colour(self):
        """A volume is still drawn as the generation it is."""
        from apps.libraries.services.operations import mounting
        from apps.libraries.services.core import success_result

        slots = [{'slot_num': 1, 'barcode': 'I60001L7', 'full': True,
                  'generation_token': 'lto-7', 'tape_lto': 'LTO-7',
                  'ltfs': True, 'ltfs_mark': 'ltfs'}]
        payload = success_result('drawn', {
            'library_id': 60, 'device_path': '/dev/sg6', 'drives': [],
            'storage_slots': slots, 'import_export_slots': [],
            'generations_present': palette.present_in(['lto-7']),
            'slot_summary': {'total_slots': 1, 'full_slots': 1}})
        with mock.patch.object(mounting, 'mount_status', return_value=payload), \
             mock.patch.object(colour, 'enabled', return_value=True):
            code, out, err = run(['op', 'layout', '60', '--ltfs'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('48;5;250m', out, 'the tile keeps the LTO-7 fill')
        self.assertIn(f'38;5;{colour.LTFS_ACCENT}m', out, 'and gains the outline')


class StylesheetMatchesTheTableTests(SimpleTestCase):
    """The stylesheet renders the palette; it does not get to invent one.

    palette.py decides which generation is which colour. The CSS and
    mhvtl_cli/colour.py render that decision - one for a browser, one for a
    terminal. These check the browser half against the table, because a
    stylesheet quietly drifting from the service is precisely the bug this
    whole piece of work was started to remove.
    """

    CSS = (Path(__file__).resolve().parents[3] / 'static' / 'css'
           / 'mhvtl-console.css')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = cls.CSS.read_text()

    def tokens_in(self, selector):
        """The --lto-N values declared in the block that `selector` opens.

        Searched from the media-generations section onwards: the theme blocks
        at the top of the file open with the same selectors and carry none of
        these, so a plain `index` finds the wrong one.
        """
        section = self.text.index('/* Media generations.')
        start = self.text.index(selector, section)
        block = self.text[start:self.text.index('}', start)]
        return dict(re.findall(r'--lto-(\d+):\s*(#[0-9a-f]{6})', block))

    def test_the_dark_themes_carry_the_tables_dark_colour(self):
        declared = self.tokens_in('[data-theme="graphite"] {')
        for generation, entry in palette.PALETTE.items():
            number = generation.split('-')[1]
            self.assertEqual(declared.get(number), entry[3],
                             f'{generation}: stylesheet and palette disagree')

    def test_the_light_themes_carry_the_tables_light_colour(self):
        declared = self.tokens_in('[data-theme="daylight"],')
        for generation, entry in palette.PALETTE.items():
            number = generation.split('-')[1]
            self.assertEqual(declared.get(number), entry[4],
                             f'{generation}: stylesheet and palette disagree')

    def test_every_generation_has_a_rule_and_an_ink(self):
        for generation, entry in palette.PALETTE.items():
            token = entry[0]
            self.assertIn(f'.mhvtl-console .{token} {{', self.text,
                          f'{token} has no rule')
            self.assertIn(f'--{token}-ink:', self.text,
                          f'{token} has a fill and no ink to write on it')

    def test_no_generation_borrows_a_severity_colour_any_more(self):
        """--ok, --warn, --bad, --info and --accent are a severity palette,
        which is why there were only five of them. A tape generation is not a
        severity."""
        start = self.text.index('/* Media generations.')
        end = self.text.index('.mhvtl-console .lto-unknown', start)
        section = self.text[start:end]
        for severity in ('--ok', '--warn', '--bad', '--info', '--accent'):
            self.assertNotIn(f'var({severity})', section,
                             f'a generation is still drawn as {severity}')

    def test_the_unknown_entry_is_not_given_a_colour(self):
        self.assertIn('.lto-unknown { background: var(--surface-deep)',
                      self.text)


class TheRuleExistsOnceTests(TestCase):
    """The mount page may put a colour on the screen. It may not work one out.

    This is the test that stops the three copies coming back. It reads the
    RENDERED page rather than the template, because a template can look right
    and render wrong - three multi-line `{# #}` comments once passed review
    and appeared in the page.
    """

    PAGE = '/libraries/operator/mount/'

    def setUp(self):
        from django.contrib.auth import get_user_model

        # Both, in this order. The operator pages guard themselves with a
        # session flag of their own (tape_operations_views.check_login) rather
        # than with Django's auth, and force_login replaces the session - so
        # the flag has to be set after it or the page answers 302.
        user = get_user_model().objects.create_user('palette-tester',
                                                    password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def rendered(self):
        response = self.client.get(self.PAGE)
        self.assertEqual(response.status_code, 200)
        return response.content.decode('utf-8', 'replace')

    def test_the_page_does_not_work_out_a_generations_colour(self):
        page = self.rendered()
        self.assertNotIn('getLtoBadgeClass', page,
                         'the second copy of the rule is back')
        self.assertNotIn('lto-${gen}', page,
                         'the third copy of the rule is back')
        self.assertNotIn("replace('LTO-', '')", page)

    def test_it_uses_the_token_the_service_sends(self):
        page = self.rendered()
        self.assertIn('drive.generation_token', page)
        self.assertIn('slot.generation_token', page)

    def test_a_full_slot_carries_both_full_and_its_generation(self):
        """It used to carry only the generation, so `.slot-compact.full` never
        applied to a storage slot at all."""
        page = self.rendered()
        self.assertIn('`full ${slot.generation_token}`', page)

    def test_the_ltfs_classes_come_from_the_services_mark(self):
        """'' means nobody read the cartridge, which is not the same as read
        and found plain - and the service is what knows the difference."""
        page = self.rendered()
        self.assertIn('slot.ltfs_mark', page)
        self.assertNotIn('slot.ltfs === true', page)
        self.assertNotIn('slot.ltfs_was === true', page)

    def test_the_legend_is_not_three_hard_coded_generations(self):
        page = self.rendered()
        self.assertIn('generations_present', page)
        for stale in ('LTO-8 tape', 'LTO-7 tape', 'LTO-6 tape'):
            self.assertNotIn(stale, page,
                             'a generation is still named in the markup')

    def test_no_generation_swatch_carries_its_own_colour(self):
        """They used to be written out inline - var(--info-bg) for LTO-8 and
        so on - which disagreed with the tiles the moment the palette moved.

        Checked as "no legend entry names a generation in the markup at all",
        because the entries are built from the service's list now. The other
        swatches on this legend - empty slot, empty drive, loaded drive, the
        two LTFS ones - keep their inline styles and are not generations.
        """
        page = self.rendered()
        self.assertNotIn('</div> LTO-', page)
        self.assertIn('id="generation-legend"', page)


class LegendComesFromTheServiceTests(TestCase):
    """`generations_present`: the service decides what the legend lists."""

    def test_only_the_generations_the_library_holds(self):
        self.assertEqual(
            palette.present_in(['lto-8', 'lto-6', 'lto-8', '', 'lto-6']),
            [{'token': 'lto-6', 'label': 'LTO-6'},
             {'token': 'lto-8', 'label': 'LTO-8'}])

    def test_they_come_back_in_generation_order_with_ten_after_nine(self):
        tokens = ['lto-10', 'lto-9', 'lto-1']
        self.assertEqual([g['token'] for g in palette.present_in(tokens)],
                         ['lto-1', 'lto-9', 'lto-10'])

    def test_media_with_no_generation_is_labelled_not_lto_and_sorts_last(self):
        present = palette.present_in(['lto-unknown', 'lto-8'])
        self.assertEqual(present[-1], {'token': 'lto-unknown',
                                       'label': 'not LTO'})

    def test_an_empty_slot_contributes_nothing(self):
        self.assertEqual(palette.present_in(['', '', '']), [])

    def test_the_mount_map_carries_it(self):
        from apps.libraries.services.operations import mounting
        element = lambda **kw: mock.Mock(**kw)      # noqa: E731
        state = mock.Mock(
            drives=[element(number=0, barcode=None, full=False,
                            slot_origin=None)],
            slots=[element(number=1, barcode='E01001L8', full=True),
                   element(number=2, barcode='', full=False)],
            import_export=[], map_slots=[])
        configured = [{'drive_num': 0, 'model': 'ULT3580-TD6', 'vendor': 'IBM',
                       'revision': '', 'lto_generation': 'LTO-6',
                       'generation_token': 'lto-6'}]
        with mock.patch.object(mounting.mapping, 'device_for_library',
                               return_value='/dev/sg4'), \
             mock.patch.object(mounting.mtx, 'status', return_value=state), \
             mock.patch.object(mounting, 'library_drives',
                               return_value=configured):
            result = mounting.mount_status(10)
        self.assertEqual(result.data['generations_present'],
                         [{'token': 'lto-6', 'label': 'LTO-6'},
                          {'token': 'lto-8', 'label': 'LTO-8'}])
