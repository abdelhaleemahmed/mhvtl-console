"""Handing out library ids, drive ids and SCSI targets.

MHVTL puts libraries and drives in one id namespace, 0-1023, and ties a drive to
its library by the Library ID line rather than by its id. config/ids keeps the
readable convention - library 10's drives are 11, 12 ... - only while it is
safe. It used not to be: a tenth drive on library 10 was given id 20.
"""
from pathlib import Path

from django.test import TestCase

from apps.libraries.services.config import device_conf, ids

FIXTURES = Path(__file__).parent / 'fixtures'


def conf(text=''):
    return device_conf.parse(text)


def fixture():
    return device_conf.parse((FIXTURES / 'device.conf').read_text())


def library(library_id, target):
    return f'Library: {library_id} CHANNEL: 00 TARGET: {target:02d} LUN: 00\n\n'


def drive(drive_id, library_id, slot, target):
    return (f'Drive: {drive_id} CHANNEL: 00 TARGET: {target:02d} LUN: 00\n'
            f' Library ID: {library_id} Slot: {slot:02d}\n\n')


class LibraryIdTests(TestCase):
    def test_the_lowest_free_multiple_of_ten(self):
        self.assertEqual(ids.next_library_id(fixture()), 40)   # 10, 20, 30 used

    def test_an_empty_host_starts_at_ten(self):
        self.assertEqual(ids.next_library_id(conf()), 10)

    def test_a_gap_is_reused(self):
        self.assertEqual(ids.next_library_id(conf(library(10, 0) + library(30, 1))), 20)

    def test_an_id_a_drive_holds_is_not_offered(self):
        """Libraries and drives share the namespace."""
        text = library(10, 0) + drive(20, 10, 1, 1)
        self.assertEqual(ids.next_library_id(conf(text)), 30)

    def test_nothing_at_or_above_1024(self):
        text = ''.join(library(n, n // 10) for n in range(10, 1030, 10))
        self.assertIsNone(ids.next_library_id(conf(text)))


class DriveIdTests(TestCase):
    def test_library_id_plus_slot_when_it_is_free(self):
        self.assertEqual(ids.drive_ids(conf(), 40, [1, 2, 3]), [41, 42, 43])

    def test_the_tenth_drive_does_not_take_the_next_library_id(self):
        """library_id + slot would be 20, library 20's id."""
        chosen = ids.drive_ids(fixture(), 10, [5, 6, 7, 8, 9, 10])
        self.assertEqual(chosen[:5], [15, 16, 17, 18, 19])
        self.assertNotIn(20, chosen)
        self.assertEqual(chosen[5], 25)     # 20 is a library, 21-24 its drives

    def test_multiples_of_ten_are_never_handed_to_drives(self):
        chosen = ids.drive_ids(conf(), 10, range(1, 40))
        self.assertFalse([n for n in chosen if n % 10 == 0])
        self.assertEqual(len(set(chosen)), 39)

    def test_taken_ids_are_skipped(self):
        text = library(10, 0) + drive(11, 10, 1, 1)
        self.assertEqual(ids.drive_ids(conf(text), 10, [1]), [12])

    def test_ids_promised_in_the_same_call_are_not_repeated(self):
        chosen = ids.drive_ids(conf(), 10, [1, 1, 1])
        self.assertEqual(len(set(chosen)), 3)

    def test_the_search_wraps_below_the_library(self):
        text = ''.join(drive(n, 1010, 1, 0) for n in range(1011, 1024) if n % 10)
        chosen = ids.drive_ids(conf(text), 1010, [1])
        self.assertLess(chosen[0], 1010)

    def test_running_out_is_an_error_that_says_so(self):
        text = ''.join(drive(n, 1, 1, 0) for n in range(1, 1024) if n % 10)
        with self.assertRaises(ids.OutOfIds) as refusal:
            ids.drive_ids(conf(text), 10, [1])
        self.assertIn('no free drive id', str(refusal.exception))


class TargetTests(TestCase):
    def test_contiguous_above_the_highest_in_use(self):
        self.assertEqual(ids.targets(fixture(), 3), (18, 19, 20))   # 0-17 used

    def test_an_empty_host_starts_at_zero(self):
        self.assertEqual(ids.targets(conf(), 2), (0, 1))

    def test_the_cap_is_99_not_50(self):
        """50 came from the old fork; MHVTL has no target limit, and 99 keeps
        the NAA line's fields at two digits."""
        self.assertEqual(device_conf.MAX_TARGET, 99)
        text = library(10, 60)
        self.assertEqual(ids.targets(conf(text), 2), (61, 62))

    def test_a_free_run_below_is_used_when_above_is_full(self):
        text = library(10, 0) + library(20, 99)
        self.assertEqual(ids.targets(conf(text), 3), (1, 2, 3))

    def test_scattered_targets_are_used_when_no_run_is_long_enough(self):
        text = ''.join(library(n * 10, t) for n, t in
                       enumerate([t for t in range(100) if t not in (5, 7, 9)], 1))
        self.assertEqual(ids.targets(conf(text), 3), (5, 7, 9))

    def test_free_targets_counts_what_is_left(self):
        """The fixture uses 15 targets between 0 and 17, of 0-99."""
        self.assertEqual(len(fixture().used_targets()), 15)
        self.assertEqual(ids.free_targets(fixture()), 85)
        self.assertEqual(ids.free_targets(conf()), 100)

    def test_too_few_free_targets_is_an_error(self):
        text = ''.join(library(n * 10, t) for n, t in enumerate(range(99), 1))
        with self.assertRaises(ids.OutOfIds):
            ids.targets(conf(text), 2)


class PlanTests(TestCase):
    def test_a_plan_is_one_target_for_the_library_then_one_per_drive(self):
        target, drive_ids, drive_targets = ids.plan_library(fixture(), 40, 2)
        self.assertEqual(target, 18)
        self.assertEqual(drive_ids, [41, 42])
        self.assertEqual(drive_targets, [19, 20])
