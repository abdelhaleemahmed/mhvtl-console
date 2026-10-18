"""The volume statistics log page, parsed.

Every page below was captured from a real drive on this host. That matters
more here than it usually does: the two drives disagree about what unit the
capacity pair is counted in, and a page written by hand would have agreed
with whatever was assumed.
"""
from unittest import mock

from django.test import TestCase

from apps.libraries.services.operations import logsense


#: IBM ULT3580-TD8 in library 50, 401 MiB onto a 500 MiB cartridge. The
#: capacity pair really is in megabytes here, as the label claims.
LTO = """    IBM       ULT3580-TD8       0108
  Volume statistics page (ssc-4), subpage=0
    Page valid: 1
    Last mount megabytes written: 422
    Last mount megabytes read: 0
    Lifetime megabytes written: 900
    Lifetime megabytes read: 0
    Last load write compression ratio: 0
    Last load read compression ratio: 0
    Total native capacity [MB]: 500
    Total used native capacity [MB]: 401
"""

#: STK T10000B in library 30, the same two fields in BYTES, with the same
#: "[MB]" label. 221 MB onto a 500 MB cartridge.
T10000 = """      STK       T10000B           0108
  Volume statistics page (ssc-4), subpage=0
    Page valid: 1
    Last mount megabytes written: 0
    Lifetime megabytes written: 221
    Last load write compression ratio: 0
    Total native capacity [MB]: 524288000
    Total used native capacity [MB]: 221089482
"""

#: No cartridge: MHVTL reports 0xFFFFFFFE for both, a placeholder it uses to
#: keep LTFS happy rather than a real size.
EMPTY = """      STK       T10000B           0108
    Last mount megabytes written: 0
    Lifetime megabytes written: 221
    Total native capacity [MB]: 4294967294
    Total used native capacity [MB]: 4294967294
"""


class CapacityUnitTests(TestCase):
    """The unit is derived from the drive, never assumed.

    Reading 401 units as 401 bytes is how a tape four fifths full reported
    "401 B written" on camera.
    """

    def test_an_lto_drive_counts_in_two_to_the_twenty(self):
        self.assertEqual(logsense.capacity_unit(logsense.parse_page(LTO)),
                         1 << 20)

    def test_a_t10000_counts_in_bytes(self):
        self.assertEqual(logsense.capacity_unit(logsense.parse_page(T10000)), 1)

    def test_a_fresh_cartridge_still_has_a_unit(self):
        """The capacity decides it, and a capacity does not move when nothing
        has been written. An earlier version divided a counter by the used
        figure and had no answer until something had been written."""
        self.assertEqual(logsense.capacity_unit({'capacity': 500, 'used': 0}),
                         1 << 20)

    def test_rewinding_does_not_change_the_unit(self):
        """THE ONE THAT SHIPPED BROKEN, in 3.5.1.

        The first version divided a megabyte counter by the used figure, on
        the reasoning that both describe the same bytes. They do, until the
        tape is rewound: the counter keeps climbing while the used figure
        starts again. Four rewrites of 221 MB on a byte-unit drive were then
        read as 2^20 units each - 231 TERABYTES on a 500 MB cartridge.
        """
        for page in ({'capacity': 524288000, 'used': 221089482,
                      'mount_mb_written': 221},
                     {'capacity': 524288000, 'used': 221089482,
                      'mount_mb_written': 884}):
            with self.subTest(written=page['mount_mb_written']):
                self.assertEqual(logsense.capacity_unit(page), 1)
                self.assertEqual(logsense.to_stats(page).written_media,
                                 221089482)

    def test_a_twelve_terabyte_cartridge_resolves_either_way_round(self):
        """The window has to hold the largest real cartridge in both readings
        without either becoming ambiguous."""
        self.assertEqual(logsense.capacity_unit({'capacity': 11444092}), 1 << 20)
        self.assertEqual(logsense.capacity_unit({'capacity': 12000000000000}), 1)

    def test_a_capacity_that_is_neither_is_refused(self):
        """Not a cartridge this code can measure, which is different from a
        cartridge it measures wrongly."""
        self.assertIsNone(logsense.capacity_unit({'capacity': 7}))
        self.assertIsNone(logsense.capacity_unit({'capacity': 0}))


class ToStatsTests(TestCase):
    def test_an_lto_drive_reports_real_bytes(self):
        stats = logsense.to_stats(logsense.parse_page(LTO))
        self.assertTrue(stats.loaded)
        self.assertEqual(stats.written_media, 401 * (1 << 20))
        self.assertEqual(stats.used_percent, 80.2)

    def test_a_t10000_reports_real_bytes(self):
        stats = logsense.to_stats(logsense.parse_page(T10000))
        self.assertEqual(stats.written_media, 221089482)
        self.assertEqual(stats.used_percent, 42.2)

    def test_the_percentage_is_right_whatever_the_unit(self):
        """It always was - both sides of the division carry the same unit.
        This pins it, because it is the reason the bug was invisible."""
        for page in (LTO, T10000):
            with self.subTest(page=page.splitlines()[0].strip()):
                stats = logsense.to_stats(logsense.parse_page(page))
                self.assertGreater(stats.used_percent, 0)
                self.assertLess(stats.used_percent, 100)

    def test_an_empty_drive_is_not_a_full_one(self):
        """capacity equals used at the placeholder, so without reading it as
        "no cartridge" every empty drive reports itself 100% full."""
        stats = logsense.to_stats(logsense.parse_page(EMPTY))
        self.assertFalse(stats.loaded)
        self.assertIsNone(stats.used_percent)

    def test_a_loaded_cartridge_nobody_has_written_to(self):
        stats = logsense.to_stats({'capacity': 500, 'used': 0,
                                   'mount_mb_written': 0})
        self.assertTrue(stats.loaded)
        self.assertEqual(stats.written_media, 0)

    def test_an_empty_page_is_no_answer(self):
        self.assertIsNone(logsense.to_stats({}))


class ReadTests(TestCase):
    def _read(self, ok=True, out='', err=''):
        with mock.patch.object(logsense.mapping, 'device_for_drive',
                               return_value='/dev/sg19'), \
                mock.patch.object(logsense.shell, 'sudo') as ran:
            ran.return_value = mock.Mock(ok=ok, stdout=out, stderr=err,
                                         output=out or err)
            return logsense.read(51), ran

    def test_a_drive_with_no_node_answers_nothing(self):
        with mock.patch.object(logsense.mapping, 'device_for_drive',
                               return_value=None):
            self.assertIsNone(logsense.read(51))

    def test_a_failed_command_answers_nothing(self):
        """None is "no answer", never "nothing written" - a caller that cannot
        tell those apart reports a busy drive as idle."""
        stats, _ = self._read(ok=False, err='no such device')
        self.assertIsNone(stats)

    def test_a_unit_attention_is_retried(self):
        """sg_logs prints the vendor and model to STDOUT even when it failed,
        so CommandResult.output - which prefers stdout - never shows the sense
        data on stderr. A check against output alone never matched, and every
        first poll after a daemon restart reported the library silent."""
        answers = [mock.Mock(ok=False, stdout='  STK  T10000B',
                             stderr='log_sense: unit attention',
                             output='  STK  T10000B'),
                   mock.Mock(ok=True, stdout=T10000, stderr='', output=T10000)]
        with mock.patch.object(logsense.mapping, 'device_for_drive',
                               return_value='/dev/sg19'), \
                mock.patch.object(logsense.shell, 'sudo',
                                  side_effect=answers) as ran:
            stats = logsense.read(51)
        self.assertEqual(ran.call_count, 2)
        self.assertEqual(stats.written_media, 221089482)
