"""What this host holds, counted once and printed by both front ends.

The console showed a panel called "MHVTL Discovery Integration" with five
numbers in it, and every one of them had always been zero. Two faults, one
symptom:

- the numbers came from ``discovered_libraries``, ``created_libraries``,
  ``total_drives``, ``total_media`` and ``recent_operations``, context keys
  ``LibraryListView`` never set, so they all rendered ``|default:0``;
- the sixty-second refresh behind them read ``data.discovered_count`` and
  friends off an endpoint that returns none of those keys, so it wrote
  ``0`` over ``0`` through ``|| 0``.

Under both was the direction. The endpoint counted rows in the *database* -
through a ``discovery_status`` nothing sets and a ``media_found`` that was
the literal ``0`` - while ``device.conf`` is the authority and the database
is a view of it. Asked on a host with seven libraries, twenty-one drives and
156 cartridges, it answered 7, 21 and 0.

So the counting moved to a service that reads the file, and the sentence it
composes is printed by ``mhvtl library list`` as well, because two front ends
that count separately are two front ends that will eventually disagree.
"""
import shutil
from pathlib import Path

from .base import TestCase
from apps.libraries.services.libraries import LibraryService

FIXTURES = Path(__file__).parent / 'fixtures'


class SummaryTests(TestCase):
    """The service's answer, against a device.conf it can be checked against."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)
        self.service = LibraryService(str(self.config))

    def test_it_counts_what_the_listing_lists(self):
        """Not a second opinion: the same call, added up."""
        listed = self.service.list().data['libraries']
        summary = self.service.summary().data

        def total(key):
            return sum(row[key] for row in listed if row[key] is not None)

        self.assertEqual(summary['libraries'], len(listed))
        self.assertEqual(summary['drives'], total('drives'))
        self.assertEqual(summary['cartridges'], total('tape_count'))
        self.assertEqual(summary['slots'], total('slot_count'))

    def test_a_library_it_cannot_read_is_said_rather_than_counted_as_empty(self):
        """This fixture is a device.conf with no contents files beside it,
        which is what an unreadable library looks like: the cartridges are
        not zero, they are unknown. Counting them as zero is the mistake the
        panel this replaces made with every number it had."""
        summary = self.service.summary().data
        listed = self.service.list().data['libraries']
        self.assertEqual(summary['unreadable'],
                         sum(1 for row in listed if row['tape_count'] is None))
        if summary['unreadable']:
            self.assertIn('could not be read', summary['says'])

    def test_the_free_slots_are_the_ones_without_a_cartridge(self):
        summary = self.service.summary().data
        self.assertEqual(summary['free_slots'],
                         summary['slots'] - summary['cartridges'])

    def test_it_says_so_in_a_sentence(self):
        """Composed in the service, because both front ends print it."""
        summary = self.service.summary().data
        self.assertEqual(summary['says'], self.service.summary().message)
        for number in ('libraries', 'drives', 'cartridges', 'slots'):
            self.assertIn(str(summary[number]), summary['says'])

    def test_a_host_with_no_libraries_says_that_rather_than_zero(self):
        """Four zeros in a row is not an answer to "what is on this host"."""
        empty = self.tmpdir()
        (empty / 'device.conf').write_text('')
        summary = LibraryService(str(empty)).summary()
        self.assertTrue(summary.success)
        self.assertEqual(summary.data['libraries'], 0)
        self.assertIn('No libraries', summary.data['says'])


class BothFrontEndsTests(TestCase):
    """The console and the terminal, told by the same service."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def test_the_command_line_ends_its_listing_with_the_sentence(self):
        from .test_cli import run

        with self.settings(MHVTL_CONFIG_DIR=str(self.config)):
            says = LibraryService(str(self.config)).summary().data['says']
            code, out, err = run(['--config-dir', str(self.config),
                                  'library', 'list'])
        self.assertEqual(code, 0)
        self.assertIn(says, out)

    def test_quietly_it_prints_the_table_alone(self):
        """For a script reading the columns."""
        from .test_cli import run

        code, out, err = run(['--quiet', '--config-dir', str(self.config),
                              'library', 'list'])
        self.assertEqual(code, 0)
        self.assertNotIn('free', out)


class TheOldPanelIsGoneTests(TestCase):
    """The page cannot go back to showing zeros it was never given."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('strip', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def pages(self):
        for path in ('/libraries/', '/libraries/list/'):
            yield path, self.client.get(path).content.decode()

    def test_neither_page_still_carries_the_discovery_panel(self):
        for path, body in self.pages():
            with self.subTest(page=path):
                self.assertNotIn('MHVTL Discovery Integration', body)
                self.assertNotIn('discovery-stat-number', body)

    def test_neither_page_polls_an_endpoint_that_no_longer_exists(self):
        """Both fetched /ajax/discovery-stats/ every sixty seconds, and read
        keys off it that it never returned."""
        for path, body in self.pages():
            with self.subTest(page=path):
                self.assertNotIn('discovery-stats', body)
                self.assertNotIn('discovered_count', body)

    def test_both_pages_show_what_the_service_counted(self):
        from apps.libraries.services.libraries import LibraryService

        summary = LibraryService().summary()
        if not summary.success:                 # no readable device.conf here
            self.skipTest('device.conf is not readable in this environment')
        for path, body in self.pages():
            with self.subTest(page=path):
                self.assertIn('host-summary', body)
                self.assertIn(str(summary.data['cartridges']), body)
