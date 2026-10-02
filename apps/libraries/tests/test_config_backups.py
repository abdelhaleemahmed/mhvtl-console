"""Listing, removing and pruning the backups every config write takes.

Nothing ever looked at them before this. On the lab host 949 had collected - 946
dated directories and three files from an older shape - and `config restore` took
a directory name the operator had to already know. The service answers both
questions now, so the console and the CLI are the same two callers as everywhere
else.
"""
import tempfile
from pathlib import Path
from unittest import mock

from .base import TestCase
from django.urls import reverse

from apps.libraries.services.config import service as config_service
from apps.libraries.services.config.service import ConfigService


class BackupListingTests(TestCase):

    def setUp(self):
        self.config = self.tmpdir()
        self.root = self.config / 'backups'

    def make(self, name, files=('device.conf',), size=10):
        target = self.root / name
        target.mkdir(parents=True, exist_ok=True)
        for each in files:
            (target / each).write_text('x' * size)
        return target

    def service(self):
        return ConfigService(self.config)

    def test_no_backup_directory_is_not_an_error(self):
        result = self.service().backups()
        self.assertTrue(result.success)
        self.assertEqual(result.data['backups'], [])
        self.assertEqual(result.data['count'], 0)

    def test_newest_first(self):
        for name in ('20260101_010101', '20260301_030303', '20260201_020202'):
            self.make(name)
        rows = self.service().backups().data['backups']
        self.assertEqual([r['name'] for r in rows],
                         ['20260301_030303', '20260201_020202',
                          '20260101_010101'])

    def test_a_row_says_when_and_what_it_holds(self):
        self.make('20260301_030303',
                  files=('device.conf', 'library_contents.10'), size=100)
        row = self.service().backups().data['backups'][0]
        self.assertEqual(row['taken'], '2026-03-01 03:03:03')
        self.assertEqual(row['file_count'], 2)
        self.assertEqual(row['bytes'], 200)
        self.assertTrue(row['has_device_conf'])
        self.assertTrue(row['recognised'])

    def test_a_backup_without_device_conf_says_so(self):
        """It cannot be usefully restored from, and the operator choosing which
        to keep should be told before they choose."""
        self.make('20260301_030303', files=('mhvtl.conf',))
        self.assertFalse(
            self.service().backups().data['backups'][0]['has_device_conf'])

    def test_an_older_shape_is_listed_but_not_recognised(self):
        """Three device.conf.backup.<stamp> files predate the dated directory.
        Hiding them would hide disk usage from somebody looking for it."""
        self.root.mkdir(parents=True)
        (self.root / 'device.conf.backup.20260405_070038').write_text('old')
        row = self.service().backups().data['backups'][0]
        self.assertFalse(row['recognised'])
        self.assertEqual(row['taken'], '')

    def test_sizes_cost_one_subprocess_however_many_backups(self):
        """backups/ is 0664 root:mhvtl on the lab host - no execute bit - so a
        child cannot be stat()ed and the sizes need privilege. One call for the
        tree, not one per backup, which is media.usage_for_all()'s lesson."""
        for i in range(5):
            self.make(f'2026010{i}_010101')
        with mock.patch.object(config_service.shell, 'sudo',
                               wraps=config_service.shell.sudo) as sudo:
            self.service().backups()
        self.assertEqual(len(sudo.call_args_list), 1)

    def test_without_sizes_nothing_is_run_at_all(self):
        self.make('20260301_030303')
        with mock.patch.object(config_service.shell, 'sudo') as sudo:
            result = self.service().backups(with_sizes=False)
        sudo.assert_not_called()
        self.assertFalse(result.data['measured'])
        self.assertEqual(result.data['backups'][0]['name'], '20260301_030303')


class RemoveBackupTests(TestCase):

    def setUp(self):
        self.config = self.tmpdir()
        self.root = self.config / 'backups'
        self.service = ConfigService(self.config)

    def make(self, name):
        (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / name / 'device.conf').write_text('x')

    def test_removes_one(self):
        self.make('20260301_030303')
        result = self.service.remove_backup('20260301_030303')
        self.assertTrue(result.success, result.message)
        self.assertFalse((self.root / '20260301_030303').exists())

    def test_an_unknown_name_is_refused(self):
        self.assertFalse(self.service.remove_backup('nope').success)

    def test_a_name_is_a_name_and_never_a_path(self):
        """A separator is refused outright, so nothing can reach out of
        backups/ - no resolving, no parent checks, no escape."""
        outside = self.config / 'device.conf'
        outside.write_text('the live configuration')
        for attempt in ('../device.conf', '/etc/passwd', '..', '.',
                        'a/b', 'a\\b', ''):
            with self.subTest(name=attempt):
                self.assertFalse(self.service.remove_backup(attempt).success)
        self.assertTrue(outside.exists(), 'the live config must be untouched')


class PruneBackupsTests(TestCase):

    def setUp(self):
        self.config = self.tmpdir()
        self.root = self.config / 'backups'
        self.service = ConfigService(self.config)
        for day in range(1, 11):
            target = self.root / f'202603{day:02d}_010101'
            target.mkdir(parents=True)
            (target / 'device.conf').write_text('x' * 10)

    def names(self):
        return sorted(p.name for p in self.root.iterdir())

    def test_keeps_the_newest_and_removes_the_rest(self):
        result = self.service.prune_backups(keep=3)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.names(),
                         ['20260308_010101', '20260309_010101',
                          '20260310_010101'])

    def test_keep_must_be_at_least_one(self):
        """A prune that leaves nothing is not a prune, and defaulting the count
        would let a forgotten argument delete every backup."""
        for bad in (0, -1, None):
            with self.subTest(keep=bad):
                self.assertFalse(self.service.prune_backups(keep=bad).success)
        self.assertEqual(len(self.names()), 10)

    def test_a_dry_run_changes_nothing_and_says_what_would_go(self):
        result = self.service.prune_backups(keep=4, dry_run=True)
        self.assertTrue(result.success)
        self.assertEqual(len(result.data['would_remove']), 6)
        self.assertEqual(len(self.names()), 10)

    def test_an_older_shape_is_never_pruned(self):
        """Somebody's deliberate copy from before the dated directory existed. A
        prune is not the place to decide about it."""
        legacy = self.root / 'device.conf.backup.20260405_070038'
        legacy.write_text('old')
        self.service.prune_backups(keep=1)
        self.assertTrue(legacy.exists())
        self.assertIn('20260310_010101', self.names())


class NoExecuteBitTests(TestCase):
    """The lab host's backups/ is `drw-rwSr--` - readable, writable, and NOT
    traversable by the group.

    Path.exists(), .is_dir() and .is_file() all raise PermissionError on a child
    there, measured on /etc/mhvtl/backups. The first version of remove_backup()
    called exists() and gave an operator a Django traceback when they pruned from
    the page. Nothing in this module may stat a backup: existence is a question
    about the parent's listing.
    """

    def setUp(self):
        self.config = self.tmpdir()
        self.root = self.config / 'backups'
        for day in (1, 2, 3):
            target = self.root / f'202603{day:02d}_010101'
            target.mkdir(parents=True)
            (target / 'device.conf').write_text('x' * 10)
        # Read and write, no execute: the child cannot be stat()ed, the parent
        # can still be listed. The same shape as the real directory.
        self.root.chmod(0o664)
        self.addCleanup(self.root.chmod, 0o755)
        self.service = ConfigService(self.config)

    def test_a_child_really_cannot_be_stated(self):
        """The premise, asserted, so this test means something if pathlib
        changes."""
        with self.assertRaises(PermissionError):
            (self.root / '20260301_010101').exists()

    def test_listing_still_works(self):
        result = self.service.backups(with_sizes=False)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.data['count'], 3)

    def test_removing_falls_back_to_sudo_instead_of_raising(self):
        with mock.patch.object(config_service.shell, 'sudo') as sudo:
            sudo.return_value = mock.Mock(ok=True, stdout='', stderr='',
                                          output='')
            result = self.service.remove_backup('20260301_010101')
        self.assertTrue(result.success, result.message)
        self.assertEqual(sudo.call_args.args[0][:2], ['rm', '-rf'])

    def test_pruning_removes_the_batch_in_one_call(self):
        """926 backups meant 926 sudo invocations inside a page request."""
        with mock.patch.object(config_service.shell, 'sudo') as sudo:
            sudo.return_value = mock.Mock(ok=True, stdout='', stderr='',
                                          output='')
            result = self.service.prune_backups(keep=1)
        self.assertTrue(result.success, result.message)
        removals = [c for c in sudo.call_args_list
                    if c.args[0][:2] == ['rm', '-rf']]
        self.assertEqual(len(removals), 1, 'one call for the whole batch')
        self.assertEqual(len(removals[0].args[0]) - 2, 2, 'both doomed backups')

    def test_an_unknown_name_is_still_refused_without_stating_it(self):
        result = self.service.remove_backup('20991231_235959')
        self.assertFalse(result.success)
        self.assertIn('No backup named', result.message)


class CleanupPageActionTests(TestCase):
    """The cleanup page's own POST actions, dispatched by name.

    The page is server-rendered with forms and messages - no fetch(), no logic
    in JavaScript - so each action is a hidden input and a service call. Full
    Scan is the same reconcile the library list page runs through
    /libraries/ajax/run-discovery/, offered here because this is the page for
    putting the database right.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('cleanup-test', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()
        self.url = reverse('libraries:cleanup_orphaned')

    def test_full_scan_runs_the_reconcile_and_reports_its_counts(self):
        stats = {'libraries_found': 6, 'created': 1, 'updated': 2,
                 'activated': 3, 'deactivated': 4, 'drives_imported': 5,
                 'drives_activated': 6, 'drives_deactivated': 7,
                 'total_db': 6, 'total_drives': 17}
        with mock.patch('apps.libraries.services.sync.service.'
                        'sync_mhvtl_to_django', return_value=stats) as sync:
            response = self.client.post(self.url, {'action': 'full_scan'},
                                        follow=True)
        sync.assert_called_once_with()
        said = ' '.join(m.message for m in response.context['messages'])
        self.assertIn('Full scan complete', said)
        self.assertIn('4 deactivated', said)
        self.assertIn('17 drive(s)', said)

    def test_an_unreadable_device_conf_refuses_rather_than_emptying_the_database(self):
        """The sync raises ConfigUnreadable, and "could not read" is not the
        same claim as "there are no libraries"."""
        from apps.libraries.services.sync.service import ConfigUnreadable

        with mock.patch('apps.libraries.services.sync.service.'
                        'sync_mhvtl_to_django',
                        side_effect=ConfigUnreadable('device.conf is not readable')):
            response = self.client.post(self.url, {'action': 'full_scan'},
                                        follow=True)
        said = ' '.join(m.message for m in response.context['messages'])
        self.assertIn('Full scan refused', said)

    def test_a_failing_scan_is_reported_and_not_raised(self):
        with mock.patch('apps.libraries.services.sync.service.'
                        'sync_mhvtl_to_django',
                        side_effect=RuntimeError('the disk went away')):
            response = self.client.post(self.url, {'action': 'full_scan'},
                                        follow=True)
        said = ' '.join(m.message for m in response.context['messages'])
        self.assertIn('Full scan failed', said)

    def test_prune_needs_a_number(self):
        response = self.client.post(self.url,
                                    {'action': 'prune_backups', 'keep': 'lots'},
                                    follow=True)
        said = ' '.join(m.message for m in response.context['messages'])
        self.assertIn('must be a number', said)

    def test_an_unknown_action_changes_nothing(self):
        with mock.patch('apps.libraries.services.sync.service.'
                        'sync_mhvtl_to_django') as sync:
            self.client.post(self.url, {'action': 'something-else'})
        sync.assert_not_called()
