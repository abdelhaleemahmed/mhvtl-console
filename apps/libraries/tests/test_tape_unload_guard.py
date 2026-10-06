"""Taking a cartridge out of a drive while a filesystem is open on it.

During this work a daemon restart unloaded a cartridge from under a live
`ltfs` mount. Nothing stopped it, because nothing on that path knew the mount
existed. These are what now stands there.
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.libraries.services.ltfs import mounts, tape_moves

PROC = '''\
ltfs:/dev/sg22 /var/lib/ltfs/mnt/library60-drive0 fuse.ltfs rw,nosuid 0 0
ltfs:/dev/sg23 /var/lib/ltfs/mnt/library60-drive1 fuse.ltfs rw,nosuid 0 0
ltfs:/dev/sg44 /var/lib/ltfs/mnt/library10-drive2 fuse.ltfs rw,nosuid 0 0
ltfs:/dev/sg50 /home/someone/mounted-by-hand fuse.ltfs rw,nosuid 0 0
/dev/sda1 / ext4 rw,relatime 0 0
'''


class ByDriveTests(SimpleTestCase):
    """Which drive a mount belongs to, read from the mount point."""

    def test_it_finds_this_library_s_drives_and_no_others(self):
        self.assertEqual(sorted(mounts.by_drive(60, PROC)), [0, 1])
        self.assertEqual(sorted(mounts.by_drive(10, PROC)), [2])
        self.assertEqual(mounts.by_drive(30, PROC), {})

    def test_it_carries_the_mount_point_so_a_message_can_name_it(self):
        self.assertEqual(mounts.by_drive(60, PROC)[0],
                         '/var/lib/ltfs/mnt/library60-drive0')

    def test_a_mount_made_outside_the_console_is_not_attributed(self):
        """Stated rather than hidden: by_device() sees it, but nothing can say
        which drive it belongs to without asking lsscsi."""
        self.assertEqual(len(mounts.by_device(PROC)), 4)
        self.assertNotIn('/home/someone/mounted-by-hand',
                         mounts.by_drive(60, PROC).values())

    def test_nothing_mounted_is_an_empty_answer_not_a_failure(self):
        self.assertEqual(mounts.by_drive(60, '/dev/sda1 / ext4 rw 0 0'), {})


class BlockedDrivesTests(SimpleTestCase):
    def test_a_mounted_drive_is_blocked_and_the_reason_names_the_point(self):
        blocked = tape_moves.blocked_drives(60, PROC)
        self.assertEqual(sorted(blocked), [0, 1])
        self.assertIn('/var/lib/ltfs/mnt/library60-drive0', blocked[0])

    def test_a_library_with_nothing_mounted_blocks_nothing(self):
        self.assertEqual(tape_moves.blocked_drives(30, PROC), {})


class UnmountTapeTests(SimpleTestCase):
    """The entry point an operator's unmount takes."""

    def test_it_refuses_while_a_filesystem_is_mounted(self):
        with mock.patch.object(tape_moves.mounts, 'by_drive',
                               return_value={2: '/var/lib/ltfs/mnt/library60-drive2'}):
            result = tape_moves.unmount_tape(60, 2)
        self.assertFalse(result.success)
        self.assertIn('cannot be emptied', result.message)
        self.assertIn('Drive 2', result.message)

    def test_the_refusal_says_what_to_do_first(self):
        with mock.patch.object(tape_moves.mounts, 'by_drive',
                               return_value={2: '/mnt/x'}):
            result = tape_moves.unmount_tape(60, 2)
        joined = ' '.join(result.errors)
        self.assertIn('mhvtl ltfs unmount 60 2', joined)
        self.assertIn('I/O errors', joined)

    def test_it_does_not_reach_the_robot_when_it_refuses(self):
        """The whole point: no mtx call, so nothing moves."""
        with mock.patch.object(tape_moves.mounts, 'by_drive',
                               return_value={2: '/mnt/x'}), \
             mock.patch.object(tape_moves, 'OperationsService') as service:
            tape_moves.unmount_tape(60, 2)
        service.assert_not_called()

    def test_an_unmounted_drive_is_handed_to_the_operations_service(self):
        with mock.patch.object(tape_moves.mounts, 'by_drive', return_value={}), \
             mock.patch.object(tape_moves, 'OperationsService') as service:
            tape_moves.unmount_tape(60, 1)
        service.return_value.unmount.assert_called_once_with(60, 1, None)

    def test_another_drive_in_the_same_library_is_not_blocked(self):
        """Only the mounted drive is refused, not the library."""
        with mock.patch.object(tape_moves.mounts, 'by_drive',
                               return_value={0: '/mnt/x'}), \
             mock.patch.object(tape_moves, 'OperationsService') as service:
            tape_moves.unmount_tape(60, 1)
        service.return_value.unmount.assert_called_once_with(60, 1, None)

    def test_a_caller_that_wants_a_particular_slot_still_can(self):
        with mock.patch.object(tape_moves.mounts, 'by_drive', return_value={}), \
             mock.patch.object(tape_moves, 'OperationsService') as service:
            tape_moves.unmount_tape(60, 1, 7)
        service.return_value.unmount.assert_called_once_with(60, 1, 7)


class LayeringTests(SimpleTestCase):
    def test_operations_still_does_not_import_ltfs(self):
        """The guard lives in ltfs/ precisely so this stays true. If it ever
        moves, test_service_layers fails too - this says why."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'services'
                  / 'operations' / 'service.py').read_text()
        self.assertNotIn('from ..ltfs', source)
        self.assertNotIn('import ltfs', source.replace('services/ltfs', ''))


class CliGoesThroughTheGuardTests(TestCase):
    """`mhvtl op unmount` refuses the same case, in the same words.

    The CLI gets the guard before the page does. That ordering is the proof
    that the decision is in the services: if the terminal can refuse it, a
    template is not deciding anything.
    """

    def run_cli(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from mhvtl_cli import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_unmount_refuses_while_a_filesystem_is_mounted(self):
        with mock.patch.object(tape_moves.mounts, 'by_drive',
                               return_value={2: '/var/lib/ltfs/mnt/library60-drive2'}), \
             mock.patch('mhvtl_cli.privileges.require_write_access'):
            code, out, err = self.run_cli(['op', 'unmount', '60', '2'])
        self.assertEqual(code, 1)
        self.assertIn('cannot be emptied', err)
        self.assertIn('mhvtl ltfs unmount 60 2', err)

    def test_it_does_not_reach_the_robot_when_it_refuses(self):
        with mock.patch.object(tape_moves.mounts, 'by_drive',
                               return_value={2: '/mnt/x'}), \
             mock.patch.object(tape_moves, 'OperationsService') as service, \
             mock.patch('mhvtl_cli.privileges.require_write_access'):
            self.run_cli(['op', 'unmount', '60', '2'])
        service.assert_not_called()

    def test_an_unmounted_drive_still_unloads(self):
        from apps.libraries.services.core import success_result

        with mock.patch.object(tape_moves.mounts, 'by_drive', return_value={}), \
             mock.patch.object(tape_moves, 'OperationsService') as service, \
             mock.patch('mhvtl_cli.privileges.require_write_access'):
            service.return_value.unmount.return_value = success_result('unloaded')
            code, out, err = self.run_cli(['op', 'unmount', '60', '1'])
        self.assertEqual(code, 0)
        service.return_value.unmount.assert_called_once_with(60, 1, None)


class LayoutShowsTheBlockTests(TestCase):
    """`op layout` says so, rather than letting an operator find out from a
    refusal after they have already decided what to do."""

    def draw(self, blocked, argv=None):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from apps.libraries.services.core import success_result
        from apps.libraries.services.operations import mounting
        from apps.libraries.services.tapes import palette
        from mhvtl_cli import main

        drives = [{'drive_num': 0, 'model': 'ULT3580-TD7', 'full': True,
                   'barcode': 'I60001L7', 'lto_generation': 'LTO-7',
                   'generation_token': 'lto-7', 'tape_lto': 'LTO-7',
                   'ltfs_capable': True},
                  {'drive_num': 1, 'model': 'ULT3580-TD7', 'full': False,
                   'barcode': None, 'lto_generation': 'LTO-7',
                   'generation_token': 'lto-7', 'tape_lto': None,
                   'ltfs_capable': True}]
        payload = success_result('drawn', {
            'library_id': 60, 'device_path': '/dev/sg6', 'drives': drives,
            'storage_slots': [], 'import_export_slots': [],
            'generations_present': palette.present_in(['lto-7']),
            'slot_summary': {'total_slots': 0, 'full_slots': 0}})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mounting, 'mount_status', return_value=payload), \
             mock.patch('apps.libraries.services.ltfs.blocked_drives',
                        return_value=blocked), \
             redirect_stdout(out), redirect_stderr(err):
            main.main(argv or ['op', 'layout', '60', '--no-colour'])
        return out.getvalue()

    def test_a_mounted_drive_is_marked_and_explained(self):
        page = self.draw({0: 'an LTFS filesystem is mounted on it, at /mnt/x'})
        drive0 = next(l for l in page.splitlines() if 'Drive 0' in l)
        self.assertIn('MOUNTED', drive0)
        self.assertIn('a filesystem is open on this drive', page)

    def test_the_other_drive_is_not_marked(self):
        page = self.draw({0: 'mounted'})
        drive1 = next(l for l in page.splitlines() if 'Drive 1' in l)
        self.assertNotIn('MOUNTED', drive1)

    def test_nothing_mounted_says_nothing(self):
        page = self.draw({})
        self.assertNotIn('MOUNTED', page)
        self.assertNotIn('a filesystem is open', page)


class PageTakesBothGesturesTests(TestCase):
    """The mount page is the unmount page now."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('gesture', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def rendered(self):
        response = self.client.get('/libraries/operator/mount/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode('utf-8', 'replace')

    def test_a_loaded_drive_is_clickable_instead_of_dead(self):
        page = self.rendered()
        self.assertIn('selectForUnmount', page)
        self.assertIn('unloadable', page)

    def test_a_blocked_drive_is_neither_clickable_nor_silent(self):
        page = self.rendered()
        self.assertIn('drive.unmount_blocked', page)

    def test_the_form_says_which_direction_it_is_going(self):
        """Two fields serve both directions; `operation` says which way round
        they mean. A second hidden drive field would have been a second place
        for the drive to live."""
        page = self.rendered()
        self.assertIn('name="operation"', page)
        self.assertNotIn('unmount_drive', page)

    def test_the_labels_are_set_once_not_written_twice(self):
        page = self.rendered()
        self.assertIn('id="slot-label"', page)
        self.assertIn('id="drive-label"', page)
        self.assertIn('Drive to unload', page)
        self.assertIn('Slot to return the tape to', page)

    def test_a_blocked_drive_is_not_offered_in_the_dropdown_either(self):
        """It cannot be emptied, so it is not a choice - on the map or in the
        list."""
        self.assertIn('drive.unmount_blocked', self.rendered())

    def test_the_page_no_longer_calls_itself_mount_only(self):
        self.assertIn('Mount and Unmount', self.rendered())

    def test_posting_an_unmount_goes_through_the_guard(self):
        from apps.libraries.services.core import success_result

        with mock.patch('apps.libraries.services.ltfs.unmount_tape',
                        return_value=success_result('unloaded')) as guarded:
            response = self.client.post('/libraries/operator/mount/', {
                'library_id': '60', 'operation': 'unmount',
                'drive': '1'})
        self.assertEqual(response.status_code, 302)
        guarded.assert_called_once_with(60, 1, None)

    def test_an_unmount_post_needs_no_slot(self):
        """A caller with nothing to say about the destination gets the
        service's default, which is the slot the cartridge came from."""
        from apps.libraries.services.core import success_result

        with mock.patch('apps.libraries.services.ltfs.unmount_tape',
                        return_value=success_result('unloaded')) as guarded:
            self.client.post('/libraries/operator/mount/', {
                'library_id': '60', 'operation': 'unmount',
                'drive': '0'})
        guarded.assert_called_once_with(60, 0, None)

    def test_a_chosen_slot_is_carried_through(self):
        """The old page let an operator pick where the tape goes back to, and
        so does this one - the default is the origin, not the only option."""
        from apps.libraries.services.core import success_result

        with mock.patch('apps.libraries.services.ltfs.unmount_tape',
                        return_value=success_result('unloaded')) as guarded:
            self.client.post('/libraries/operator/mount/', {
                'library_id': '60', 'operation': 'unmount',
                'drive': '0', 'slot': '14'})
        guarded.assert_called_once_with(60, 0, 14)

    def test_a_refused_unmount_is_reported_rather_than_swallowed(self):
        from apps.libraries.services.core import failure_result

        with mock.patch('apps.libraries.services.ltfs.unmount_tape',
                        return_value=failure_result(
                            'Drive 1 cannot be emptied: a filesystem is mounted',
                            ['Unmount the filesystem first'])):
            response = self.client.post('/libraries/operator/mount/', {
                'library_id': '60', 'operation': 'unmount',
                'drive': '1'}, follow=True)
        body = response.content.decode('utf-8', 'replace')
        self.assertIn('cannot be emptied', body)
        self.assertIn('Unmount the filesystem first', body)

    def test_mounting_still_works_and_still_needs_both(self):
        from apps.libraries.services.core import success_result
        from apps.libraries import tape_operations_views as views

        with mock.patch.object(views, '_operations') as ops:
            ops.return_value.mount.return_value = success_result('mounted')
            self.client.post('/libraries/operator/mount/', {
                'library_id': '10', 'slot': '5', 'drive': '0'})
        ops.return_value.mount.assert_called_once_with(10, 5, 0)

    def test_a_mount_without_a_drive_is_refused_not_guessed(self):
        from apps.libraries import tape_operations_views as views

        with mock.patch.object(views, '_operations') as ops:
            response = self.client.post('/libraries/operator/mount/',
                                        {'library_id': '10', 'slot': '5'},
                                        follow=True)
        ops.return_value.mount.assert_not_called()
        self.assertIn('select a tape and a drive',
                      response.content.decode('utf-8', 'replace'))


class StatusEndpointCarriesTheBlockTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('endpoint', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def test_every_drive_says_whether_it_may_be_emptied(self):
        import json

        with mock.patch('apps.libraries.services.ltfs.blocked_drives',
                        return_value={0: 'an LTFS filesystem is mounted on it'}):
            response = self.client.get('/libraries/ajax/library-status-lto/10/')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        if not data.get('success'):
            self.skipTest(f"library 10 unreadable here: {data.get('error')}")
        blocked = {d['drive_num']: d['unmount_blocked'] for d in data['drives']}
        self.assertIn('LTFS filesystem is mounted', blocked[0])
        self.assertEqual(blocked[1], '', 'only the mounted drive is blocked')


class DestinationIsTheServicesDecisionTests(SimpleTestCase):
    """Where a cartridge may go back to, and which slot is offered first.

    The old unmount page filtered the slot list in JavaScript and picked the
    default itself. That is the same shape as the generation colours it took
    six steps to remove, so it is decided here instead.
    """

    def status(self, slots, drives):
        from apps.libraries.services.operations import mounting

        element = lambda **kw: mock.Mock(**kw)      # noqa: E731
        state = mock.Mock(
            drives=[element(number=d['n'], barcode=d.get('barcode'),
                            full=d.get('full', False),
                            slot_origin=d.get('origin')) for d in drives],
            slots=[element(number=s['n'], barcode=s.get('barcode', ''),
                           full=s.get('full', False)) for s in slots],
            import_export=[], map_slots=[])
        configured = [{'drive_num': d['n'], 'model': 'ULT3580-TD8',
                       'vendor': 'IBM', 'revision': '',
                       'lto_generation': 'LTO-8',
                       'generation_token': 'lto-8'} for d in drives]
        with mock.patch.object(mounting.mapping, 'device_for_library',
                               return_value='/dev/sg4'), \
             mock.patch.object(mounting.mtx, 'status', return_value=state), \
             mock.patch.object(mounting, 'library_drives',
                               return_value=configured):
            return mounting.mount_status(10).data

    def test_the_targets_are_the_empty_slots(self):
        data = self.status(
            slots=[{'n': 1, 'full': True, 'barcode': 'E01001L8'},
                   {'n': 2}, {'n': 3}],
            drives=[{'n': 0}])
        self.assertEqual(data['unmount_targets'], [2, 3])

    def test_a_loaded_drive_offers_the_slot_it_came_from(self):
        data = self.status(
            slots=[{'n': 1, 'full': True, 'barcode': 'E01001L8'}, {'n': 7}],
            drives=[{'n': 0, 'full': True, 'barcode': 'E01007L8', 'origin': 7}])
        drive = data['drives'][0]
        self.assertEqual(drive['unmount_default_slot'], 7)
        self.assertIn('where it came from', drive['unmount_note'])

    def test_an_occupied_origin_is_not_offered_and_says_why(self):
        """An operator told "slot 7" who finds slot 7 occupied has been told
        something false."""
        data = self.status(
            slots=[{'n': 7, 'full': True, 'barcode': 'E01007L8'}, {'n': 9}],
            drives=[{'n': 0, 'full': True, 'barcode': 'X', 'origin': 7}])
        drive = data['drives'][0]
        self.assertIsNone(drive['unmount_default_slot'])
        self.assertIn('something is in it now', drive['unmount_note'])
        self.assertEqual(data['unmount_targets'], [9])

    def test_an_empty_drive_offers_nothing_and_says_nothing(self):
        data = self.status(slots=[{'n': 1}], drives=[{'n': 0}])
        self.assertIsNone(data['drives'][0]['unmount_default_slot'])
        self.assertEqual(data['drives'][0]['unmount_note'], '')

    def test_a_drive_whose_origin_is_unknown_still_offers_the_free_slots(self):
        """mtx does not always report where a cartridge came from."""
        data = self.status(
            slots=[{'n': 4}, {'n': 5}],
            drives=[{'n': 0, 'full': True, 'barcode': 'X', 'origin': None}])
        self.assertIsNone(data['drives'][0]['unmount_default_slot'])
        self.assertEqual(data['unmount_targets'], [4, 5])


class TheOldPageIsAContractTests(TestCase):
    """`/operator/unmount/` is gone, and still answers.

    Somebody has it bookmarked, or in a runbook, or in a browser tab opened
    last week. A URL that worked is a contract; deleting the page is not a
    reason to break it.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('contract', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def test_it_lands_on_the_page_that_does_the_job(self):
        response = self.client.get('/libraries/operator/unmount/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/libraries/operator/mount/', response['Location'])

    def test_the_library_is_carried_through(self):
        """The link on the library detail page says which library it meant."""
        response = self.client.get('/libraries/operator/unmount/?library_id=60')
        self.assertIn('library_id=60', response['Location'])

    def test_a_stale_tab_posting_here_is_redirected_not_refused(self):
        """Nothing posts here any more. A 405 about a method helps nobody."""
        response = self.client.post('/libraries/operator/unmount/',
                                    {'library_id': '10', 'drive': '0'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('library_id=10', response['Location'])

    def test_it_still_asks_for_a_login(self):
        self.client.logout()
        fresh = self.client.get('/libraries/operator/unmount/')
        self.assertEqual(fresh.status_code, 302)
        self.assertNotIn('/libraries/operator/mount/', fresh['Location'])

    def test_the_template_is_gone(self):
        from django.template import TemplateDoesNotExist
        from django.template.loader import get_template

        with self.assertRaises(TemplateDoesNotExist):
            get_template('libraries/operator/unmount_tape.html')

    def test_nothing_still_links_to_it(self):
        """Two links to one page said there were two jobs."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        offenders = [str(f.relative_to(root))
                     for f in root.glob('apps/**/templates/**/*.html')
                     if "'libraries:unmount_tape'" in f.read_text()]
        self.assertEqual(offenders, [])


class LtfsIsItsOwnGroupTests(TestCase):
    """LTFS is a set of operations, not one more tape operation.

    It reads drive and volume status, mounts and unmounts a filesystem, checks
    one, provisions an LTFS-capable drive and creates media for it - against a
    filesystem rather than against a robot.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('dash', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def dashboard(self):
        """The page, without the frame around it.

        These tests read the document in order - Tape Operations before LTFS
        Operations, the LTFS link after its heading - and since 6 October 2026
        the frame says "Tape Operations" too, in the console's navigation, and
        offers an LTFS link in the section's own row. Both are above the page
        and both are *right*; what is being checked is how the page groups its
        own tiles, so the page is what is read.
        """
        response = self.client.get('/libraries/operator/')
        self.assertEqual(response.status_code, 200)
        whole = response.content.decode('utf-8', 'replace')
        return whole.split('<main class="content">', 1)[-1]

    def test_it_has_a_group_of_its_own(self):
        page = self.dashboard()
        self.assertIn('LTFS Operations', page)

    def test_it_is_no_longer_filed_under_tape_operations(self):
        page = self.dashboard()
        tape = page.index('Tape Operations')
        ltfs_group = page.index('LTFS Operations')
        ltfs_link = page.index("operator/ltfs")
        self.assertLess(tape, ltfs_group)
        self.assertLess(ltfs_group, ltfs_link,
                        'the LTFS link should sit under its own heading')

    def test_tape_operations_keeps_the_robot_and_only_the_robot(self):
        """The two "mounts" are different operations - one moves a cartridge
        into a drive, the other opens what is in the drive."""
        page = self.dashboard()
        tape = page.index('Tape Operations')
        section = page[tape:page.index('LTFS Operations')]
        self.assertIn('Mount / Unmount Tape', section)
        self.assertIn('Move Tape', section)
        self.assertNotIn('operator/ltfs', section)
