"""LTO compatibility: can this cartridge go in that drive?

Step 8 of the service-layer refactor. No fakes here at all - these are table
lookups and the rule they encode, which is worth stating once in a test because
getting it wrong turns a backup into a failure at run time rather than at the
point the operator chose the drive.

The rule, as MHVTL 1.8 emulates it: LTO-3 to LTO-7 read and write their own and
the previous generation and read one more back; LTO-8 and LTO-9 handle only
their own and the previous generation; LTO-10 handles only LTO-10. These tests
used to say LTO-8 reads LTO-6 - the rule this module once stated, and the one
MHVTL does not follow.
"""
from django.test import TestCase

from apps.libraries.services.tapes import compatibility


class ReadWriteRuleTests(TestCase):
    def test_same_generation_reads_and_writes(self):
        result = compatibility.check('LTO-8', 'LTO-8')
        self.assertTrue(result['can_read'])
        self.assertTrue(result['can_write'])

    def test_one_generation_back_still_writes(self):
        result = compatibility.check('LTO-7', 'LTO-8')
        self.assertTrue(result['can_write'])

    def test_two_generations_back_is_read_only_up_to_lto7(self):
        """A restore works, a backup does not - and the message says so."""
        result = compatibility.check('LTO-5', 'LTO-7')
        self.assertTrue(result['can_read'])
        self.assertFalse(result['can_write'])
        self.assertTrue(result['compatible'])
        self.assertIn('read-only', result['message'])

    def test_lto8_does_not_read_lto6(self):
        """The table used to say it did (ult3580_pm.c td8_media: LTO7, LTO8)."""
        self.assertFalse(compatibility.check('LTO-6', 'LTO-8')['can_read'])

    def test_lto9_does_not_read_lto7(self):
        """ult3580_pm.c td9_media: LTO8, LTO9."""
        self.assertFalse(compatibility.check('LTO-7', 'LTO-9')['can_read'])

    def test_lto10_reads_only_lto10(self):
        self.assertTrue(compatibility.check('LTO-10', 'LTO-10')['can_write'])
        self.assertFalse(compatibility.check('LTO-9', 'LTO-10')['can_read'])

    def test_three_generations_back_is_not_readable(self):
        result = compatibility.check('LTO-4', 'LTO-7')
        self.assertFalse(result['can_read'])
        self.assertFalse(result['compatible'])

    def test_a_newer_tape_is_refused_with_what_to_do(self):
        result = compatibility.check('LTO-9', 'LTO-6')
        self.assertFalse(result['compatible'])
        self.assertIn('too new', result['message'])
        self.assertIn('LTO-9 drive or newer', result['message'])

    def test_an_older_tape_says_how_far_back_the_drive_reads(self):
        """A different problem from "too new", and a different fix."""
        result = compatibility.check('LTO-2', 'LTO-8')
        self.assertIn('too old', result['message'])
        self.assertIn('LTO-7', result['message'])

    def test_the_oldest_generation_only_takes_its_own(self):
        self.assertTrue(compatibility.check('LTO-1', 'LTO-1')['can_write'])
        self.assertFalse(compatibility.check('LTO-2', 'LTO-1')['can_read'])

    def test_an_unknown_generation_is_not_guessed(self):
        for tape, drive in (('LTO-8', 'LTO-12'), (None, 'LTO-8'),
                            ('LTO-8', None), ('', '')):
            with self.subTest(tape=tape, drive=drive):
                self.assertFalse(compatibility.check(tape, drive)['compatible'])


class BarcodeGenerationTests(TestCase):
    def test_reads_the_generation_from_the_suffix(self):
        self.assertEqual(compatibility.lto_for_barcode('E01001L8'), 'LTO-8')
        self.assertEqual(compatibility.lto_for_barcode('E01001L5'), 'LTO-5')

    def test_a_worm_cartridge_keeps_its_generation(self):
        """LU is LTO-4 WORM: the drive's capability is unchanged, what changes
        is whether the medium can be overwritten."""
        self.assertEqual(compatibility.lto_for_barcode('W01001LU'), 'LTO-4')

    def test_a_non_lto_barcode_has_no_generation(self):
        """A T10000 barcode is valid and simply is not LTO."""
        self.assertIsNone(compatibility.lto_for_barcode('G01001TA'))

    def test_case_and_padding_do_not_matter(self):
        self.assertEqual(compatibility.lto_for_barcode(' e01001l8 '), 'LTO-8')

    def test_an_empty_barcode_is_none_not_an_error(self):
        self.assertIsNone(compatibility.lto_for_barcode(''))
        self.assertIsNone(compatibility.lto_for_barcode(None))


class DriveModelTests(TestCase):
    def test_reads_the_generation_from_an_ibm_model(self):
        self.assertEqual(compatibility.lto_for_drive_model('ULT3580-TD8'), 'LTO-8')

    def test_reads_an_hp_model_with_a_space_in_it(self):
        self.assertEqual(compatibility.lto_for_drive_model('Ultrium 6-SCSI'),
                         'LTO-6')

    def test_an_unknown_model_is_none(self):
        self.assertIsNone(compatibility.lto_for_drive_model('SDX-900V'))


class MountableDriveTests(TestCase):
    """Which drives the operator page should offer for a given cartridge."""

    DRIVES = [
        {'drive_num': 0, 'lto_generation': 'LTO-8', 'full': False},
        {'drive_num': 1, 'lto_generation': 'LTO-6', 'full': False},
        {'drive_num': 2, 'lto_generation': 'LTO-8', 'full': True},
        {'drive_num': 3, 'lto_generation': 'LTO-4', 'full': False},
        {'drive_num': 4, 'lto_generation': 'LTO-7', 'full': False},
    ]

    def test_offers_only_drives_that_can_take_the_tape(self):
        usable = compatibility.drives_for_tape('LTO-8', self.DRIVES)
        self.assertEqual([d['drive_num'] for d in usable], [0])

    def test_a_loaded_drive_is_never_offered(self):
        """Drive 2 is an LTO-8 drive and would be compatible, but a mount into
        a full drive fails whatever the generations are."""
        usable = compatibility.drives_for_tape('LTO-8', self.DRIVES)
        self.assertNotIn(2, [d['drive_num'] for d in usable])

    def test_a_read_only_drive_is_offered_and_flagged(self):
        usable = compatibility.drives_for_tape('LTO-5', self.DRIVES)
        by_num = {d['drive_num']: d for d in usable}
        self.assertTrue(by_num[1]['can_write'], 'LTO-5 in LTO-6 is read/write')
        self.assertFalse(by_num[4]['can_write'], 'LTO-5 in LTO-7 is read-only')
        self.assertNotIn(0, by_num, 'LTO-8 does not read LTO-5')

    def test_a_tape_with_no_compatible_drive_gets_an_empty_list(self):
        self.assertEqual(compatibility.drives_for_tape('LTO-9', self.DRIVES), [])

    def test_an_unknown_tape_generation_offers_nothing(self):
        self.assertEqual(compatibility.drives_for_tape(None, self.DRIVES), [])


class VerdictTests(TestCase):
    """verdict(): any cartridge, any drive, by barcode and product string."""

    def test_an_lto_mismatch_says_which_way_round(self):
        result = compatibility.verdict('F01030L6', 'ULT3580-TD8')
        self.assertTrue(result['known'])
        self.assertFalse(result['compatible'])
        self.assertIn('too old', result['message'])

    def test_a_worm_lto_cartridge_is_judged_by_its_generation(self):
        """LY is LTO-8 WORM."""
        self.assertTrue(compatibility.verdict('W01001LY', 'ULT3580-TD9')['can_write'])

    def test_an_lto_cartridge_in_a_t10k_drive_is_refused(self):
        result = compatibility.verdict('E01001L8', 'T10000C')
        self.assertTrue(result['known'])
        self.assertFalse(result['compatible'])
        self.assertIn('T10KC', result['message'])

    def test_t10k_generations(self):
        """T10000B loads T10KA and T10KB, both read/write (t10000_pm.c:500-503)."""
        self.assertTrue(compatibility.verdict('K01001TA', 'T10000B')['can_write'])
        self.assertFalse(compatibility.verdict('K01001TC', 'T10000B')['compatible'])

    def test_9840_read_only_generation(self):
        """9840D reads 9840B and cannot write it, and does not load 9840A
        (stk9x40_pm.c:501-506)."""
        read_only = compatibility.verdict('K01001TY', 'T9840D')
        self.assertTrue(read_only['can_read'])
        self.assertFalse(read_only['can_write'])
        self.assertIn('read-only', read_only['message'])
        self.assertFalse(compatibility.verdict('K01001TZ', 'T9840D')['compatible'])

    def test_sdlt600_writes_sdlt320_and_only_reads_sdlt220(self):
        self.assertTrue(compatibility.verdict('Q01001S2', 'SDLT600')['can_write'])
        self.assertFalse(compatibility.verdict('Q01001S1', 'SDLT600')['can_write'])
        self.assertTrue(compatibility.verdict('Q01001S1', 'SDLT600')['can_read'])

    def test_3592_generations(self):
        """E07 does not load J1A cartridges (ibm_03592_pm.c init_3592_E07)."""
        self.assertFalse(compatibility.verdict('I01001JA', '03592E07')['compatible'])
        self.assertTrue(compatibility.verdict('I01001JC', '03592E07')['can_write'])

    def test_an_unknown_barcode_is_not_refused(self):
        result = compatibility.verdict('NOBAR', 'ULT3580-TD8')
        self.assertFalse(result['known'])
        self.assertTrue(result['compatible'])
        self.assertIn('MHVTL decides', result['message'])

    def test_an_unknown_drive_is_not_refused(self):
        result = compatibility.verdict('E01001L8', 'SOMETHING')
        self.assertFalse(result['known'])
        self.assertTrue(result['compatible'])

    def test_a_cleaning_cartridge_is_left_to_the_drive(self):
        result = compatibility.verdict('CLN101L8', 'ULT3580-TD6')
        self.assertTrue(result['compatible'])
        self.assertFalse(result['can_write'])

    def test_the_matrix_covers_every_loaded_slot_and_every_drive(self):
        slots = [{'slot_num': 1, 'full': True, 'barcode': 'E01001L8'},
                 {'slot_num': 2, 'full': False, 'barcode': None}]
        drives = [{'drive_num': 0, 'model': 'ULT3580-TD8', 'full': True},
                  {'drive_num': 1, 'model': 'ULT3580-TD6', 'full': False}]
        matrix = compatibility.mount_matrix(slots, drives)
        self.assertEqual(list(matrix), [1])
        self.assertTrue(matrix[1][0]['can_write'])
        self.assertFalse(matrix[1][1]['compatible'])


class MountPageTests(TestCase):
    """The operator page reads the server's verdicts and has no rule of its own.

    It used to compute "reads two back, writes one back" in JavaScript, which
    offered LTO-6 tapes to LTO-8 drives and said "too new" for tapes that were
    too old. The page's behaviour was checked in a browser DOM (jsdom) when
    this changed; this keeps the rule from coming back.
    """
    TEMPLATE = 'libraries/operator/mount_tape.html'

    def _source(self):
        from django.template.loader import get_template
        return get_template(self.TEMPLATE).template.source

    def test_the_page_reads_the_mount_matrix(self):
        source = self._source()
        self.assertIn('mount_matrix', source)
        self.assertIn('option.disabled', source)

    def test_the_page_computes_no_generations(self):
        source = self._source()
        for token in ('driveGen', 'tapeGen', "replace('LTO-', ''))"):
            self.assertNotIn(token, source)
