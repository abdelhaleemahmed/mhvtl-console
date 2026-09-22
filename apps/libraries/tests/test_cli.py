"""The command line interface.

Step 9. The CLI holds no logic of its own - a command parses arguments, calls
one service method and prints - so these tests are about the parsing, the
printing and the exit codes. The services have their own tests.

Every service call is faked. A test that ran `mhvtl library create` for real
would create a library on whatever host the suite runs on, which is the mistake
that made manual_mhvtl_service.py dangerous.
"""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core import failure_result, success_result
from mhvtl_cli import main, output, privileges

LIBRARIES = success_result('3 libraries configured', {'libraries': [
    {'library_id': 10, 'model': 'STK L700', 'serial': 'XYZZY_A', 'drives': 4,
     'slot_count': 39, 'tape_count': 32},
    {'library_id': 20, 'model': 'SONY LIB-302', 'serial': '80000020',
     'drives': 4, 'slot_count': 40, 'tape_count': 25},
], 'count': 2})


def run(argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class ParserTests(TestCase):
    def test_no_arguments_prints_help_and_is_a_usage_error(self):
        code, out, err = run([])
        self.assertEqual(code, output.EXIT_USAGE)
        self.assertIn('mhvtl', err)

    def test_a_noun_with_no_verb_prints_its_help(self):
        code, out, err = run(['library'])
        self.assertEqual(code, output.EXIT_USAGE)
        self.assertIn('list', out + err)

    def test_an_unknown_noun_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit) as exit_code:
            run(['nonsense'])
        self.assertEqual(exit_code.exception.code, 2)


class OutputTests(TestCase):
    """Data on stdout, diagnostics on stderr, so a pipe stays valid."""

    def test_a_table_is_aligned_and_has_a_rule(self):
        out = io.StringIO()
        with redirect_stdout(out):
            output.table([{'a': 'one', 'b': 'x'}, {'a': 'three', 'b': 'yy'}],
                         columns=['a', 'b'])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], 'A      B')
        self.assertTrue(set(lines[1]) <= {'-', ' '})
        self.assertEqual(lines[2], 'one    x')

    def test_no_rows_prints_nothing_at_all(self):
        out = io.StringIO()
        with redirect_stdout(out):
            output.table([], columns=['a'])
        self.assertEqual(out.getvalue(), '')

    def test_booleans_read_as_words(self):
        """True/False in a table of device paths is a Python repr."""
        out = io.StringIO()
        with redirect_stdout(out):
            output.table([{'full': True, 'x': False}], columns=['full', 'x'])
        self.assertIn('yes', out.getvalue())
        self.assertIn('no', out.getvalue())

    def test_a_missing_value_is_distinguishable_from_an_empty_one(self):
        out = io.StringIO()
        with redirect_stdout(out):
            output.table([{'a': None}], columns=['a'])
        self.assertIn(output.EMPTY, out.getvalue())

    def test_a_failure_goes_to_stderr_and_not_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = output.result(failure_result('it broke', ['because']))
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('it broke', err.getvalue())
        self.assertIn('because', err.getvalue())

    def test_json_output_is_valid_json_on_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            output.result(success_result('fine', {'n': 1}), as_json=True)
        self.assertEqual(json.loads(out.getvalue())['data'], {'n': 1})

    def test_a_failure_in_json_is_still_json(self):
        """A script parsing the output should not have to special-case errors."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = output.result(failure_result('no', ['why']), as_json=True)
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertFalse(json.loads(out.getvalue())['success'])

    def test_quiet_suppresses_the_confirmation_but_not_the_exit_code(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = output.result(success_result('done'), quiet=True)
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(out.getvalue(), '')


class LibraryCommandTests(TestCase):
    def test_list_prints_a_row_per_library(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(LibraryService, 'list', return_value=LIBRARIES):
            code, out, err = run(['library', 'list'])

        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('STK L700', out)
        self.assertIn('SONY LIB-302', out)

    def test_list_json_carries_the_service_result(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(LibraryService, 'list', return_value=LIBRARIES):
            code, out, err = run(['--json', 'library', 'list'])

        payload = json.loads(out)
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['data']['libraries']), 2)

    def test_an_empty_configuration_says_so_rather_than_printing_a_bare_table(self):
        from apps.libraries.services.libraries import LibraryService
        empty = success_result('no libraries', {'libraries': [], 'count': 0})
        with mock.patch.object(LibraryService, 'list', return_value=empty):
            code, out, err = run(['library', 'list'])

        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('No libraries', out)

    def test_an_unreadable_configuration_is_a_failure_not_an_empty_list(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(LibraryService, 'list',
                               return_value=failure_result(
                                   'cannot read device.conf', ['permission denied'])):
            code, out, err = run(['library', 'list'])

        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('cannot read device.conf', err)

    def test_next_id_prints_only_the_number(self):
        """So `mhvtl library create --id $(mhvtl library next-id)` works."""
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(LibraryService, 'next_id',
                               return_value=success_result('next is 40',
                                                           {'library_id': 40})):
            code, out, err = run(['library', 'next-id'])

        self.assertEqual(out.strip(), '40')

    def test_show_prints_the_fields_as_a_list(self):
        from apps.libraries.services.libraries import LibraryService
        library = success_result('Library 10', {'library': {
            'library_id': 10, 'vendor': 'STK', 'product': 'L700',
            'serial': 'XYZZY_A', 'channel': 0, 'target': 0, 'lun': 0,
            'naa': '10:22', 'home_directory': '/opt/mhvtl', 'drives': 4,
            'drive_ids': [11, 12], 'slot_count': 39, 'tape_count': 32}})
        with mock.patch.object(LibraryService, 'get', return_value=library):
            code, out, err = run(['library', 'show', '10'])

        self.assertIn('vendor', out)
        self.assertIn('STK', out)
        self.assertIn('11, 12', out)


class PrivilegeTests(TestCase):
    """A mutating verb refuses before it starts, naming what would allow it."""

    def test_create_is_refused_without_write_access(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(LibraryService, 'create') as create:
            code, out, err = run(['library', 'create', '--profile', 'IBM'])

        self.assertEqual(code, output.EXIT_DENIED)
        self.assertIn('mhvtl', err)
        create.assert_not_called()

    def test_delete_is_refused_without_write_access(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(LibraryService, 'delete') as delete:
            code, out, err = run(['library', 'delete', '10'])

        self.assertEqual(code, output.EXIT_DENIED)
        delete.assert_not_called()

    def test_reading_never_asks_for_write_access(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(LibraryService, 'list', return_value=LIBRARIES):
            code, out, err = run(['library', 'list'])
        self.assertEqual(code, output.EXIT_OK)

    def test_create_passes_the_specification_through(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(LibraryService, 'create',
                               return_value=success_result('created')) as create:
            code, out, err = run(['library', 'create', '--profile', 'IBM',
                                  '--id', '40', '--drives', '2', '--no-start'])

        self.assertEqual(code, output.EXIT_OK)
        spec = create.call_args[0][0]
        self.assertEqual(spec, {'profile': 'IBM', 'library_id': 40,
                                'num_drives': 2})
        self.assertFalse(create.call_args[1]['start_services'])

    def test_delete_does_not_remove_media_unless_asked(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(LibraryService, 'delete',
                               return_value=success_result('deleted')) as delete:
            run(['library', 'delete', '10'])
        self.assertFalse(delete.call_args[1]['remove_media'])

    def test_root_may_write(self):
        with mock.patch.object(privileges.os, 'geteuid', return_value=0):
            self.assertTrue(privileges.can_write())

    def test_a_member_of_the_mhvtl_group_may_write(self):
        with mock.patch.object(privileges, 'current_identity',
                               return_value={'uid': 1000, 'user': 'operator',
                                             'root': False,
                                             'groups': ['operator', 'mhvtl']}):
            self.assertTrue(privileges.can_write())

    def test_anyone_else_may_not(self):
        with mock.patch.object(privileges, 'current_identity',
                               return_value={'uid': 1000, 'user': 'nobody',
                                             'root': False,
                                             'groups': ['nobody']}):
            self.assertFalse(privileges.can_write())

    def test_the_refusal_names_the_group_to_join(self):
        with mock.patch.object(privileges, 'can_write', return_value=False):
            with self.assertRaises(privileges.PermissionDenied) as refusal:
                privileges.require_write_access('creating a library')
        message = str(refusal.exception)
        self.assertIn('creating a library', message)
        self.assertIn(privileges.SERVICE_GROUP, message)


class StatusCommandTests(TestCase):
    def test_system_reports_the_unit_state(self):
        from apps.libraries.services.console import modules, units

        state = mock.MagicMock(target_active=True, target_enabled=True,
                               libraries_active=3, drives_active=12,
                               healthy=True, stale=[])
        state.libraries = [1, 2, 3]
        state.drives = list(range(12))

        with mock.patch.object(units, 'status', return_value=state), \
             mock.patch.object(modules, 'summary', return_value={'backend': 'mhvtl'}):
            code, out, err = run(['status', 'system'])

        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('3/3 running', out)
        self.assertIn('mhvtl', out)

    def test_stale_units_are_named_with_what_removes_them(self):
        from apps.libraries.services.console import modules, units

        state = mock.MagicMock(target_active=True, target_enabled=True,
                               libraries_active=3, drives_active=12,
                               healthy=True, stale=['vtllibrary@40.service'])
        state.libraries = [1, 2, 3]
        state.drives = list(range(12))

        with mock.patch.object(units, 'status', return_value=state), \
             mock.patch.object(modules, 'summary', return_value={'backend': 'mhvtl'}):
            code, out, err = run(['status', 'system'])

        self.assertIn('vtllibrary@40.service', out)
        self.assertIn('orphans --clean', out)

    def test_activity_prints_what_the_web_page_shows(self):
        """The whole reason the wording lives in the service: this and the
        library page must say the same thing about the same drive."""
        from apps.libraries.services.drives import service as drive_service
        from apps.libraries.services.operations.vtlcmd import TapeStats

        drive_service.samples.forget()
        readings = iter([TapeStats(barcode='Q40002L8', loaded=True,
                                   written=28405760, written_media=28405760,
                                   capacity=524288000),
                         TapeStats(barcode='Q40002L8', loaded=True,
                                   written=314572800, written_media=314572800,
                                   capacity=524288000)])
        answer = success_result('4 drive(s)', {'drives': [
            {'drive_id': 41, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_40'}]}, 'op')

        with mock.patch.object(drive_service.DriveService, 'list',
                               return_value=answer), \
                mock.patch.object(drive_service.vtlcmd, 'stats',
                                  side_effect=lambda drive_id: next(readings)), \
                mock.patch.object(drive_service.time, 'sleep'):
            code, out, err = run(['status', 'activity', '40'])

        self.assertEqual(code, output.EXIT_OK, err)
        self.assertIn('writing', out)
        self.assertIn('Q40002L8', out)
        self.assertIn('300.0 MB written', out)
        self.assertIn('60.0% of the tape', out)

    def test_activity_can_be_asked_not_to_wait(self):
        """--settle 0 takes one reading and says "holding" rather than
        guessing at a rate."""
        from apps.libraries.services.drives import service as drive_service
        from apps.libraries.services.operations.vtlcmd import TapeStats

        drive_service.samples.forget()
        answer = success_result('1 drive', {'drives': [
            {'drive_id': 41, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_40'}]}, 'op')

        with mock.patch.object(drive_service.DriveService, 'list',
                               return_value=answer), \
                mock.patch.object(drive_service.vtlcmd, 'stats',
                                  return_value=TapeStats(barcode='Q40002L8',
                                                         loaded=True,
                                                         written=1024)), \
                mock.patch.object(drive_service.time, 'sleep') as slept:
            code, out, err = run(['status', 'activity', '40', '--settle', '0'])

        self.assertEqual(code, output.EXIT_OK, err)
        slept.assert_not_called()
        self.assertIn('holding', out)

    def test_a_drive_with_no_device_is_reported_clearly(self):
        from apps.libraries.services.scsi import mapping
        with mock.patch.object(mapping, 'device_for_drive', return_value=None):
            code, out, err = run(['status', 'drive', '11'])

        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('no device for drive 11', err)


class NonRewindingTests(TestCase):
    """Reading a drive's status must not move its tape."""

    def test_status_is_read_through_the_non_rewinding_node(self):
        """`mt -f /dev/st0 status` closes the device, which rewinds the tape:
        asking a drive what it is doing changes what it is doing."""
        from apps.libraries.services.operations import mt
        from apps.libraries.services.core.shell import CommandResult
        from pathlib import Path

        with mock.patch.object(Path, 'exists', return_value=True), \
             mock.patch.object(mt.shell, 'sudo',
                               return_value=CommandResult(['mt'], 0, '', '')) as sudo:
            mt.status('/dev/st0')

        self.assertEqual(sudo.call_args[0][0][2], '/dev/nst0')

    def test_a_host_without_the_node_falls_back(self):
        from apps.libraries.services.operations import mt
        from pathlib import Path
        with mock.patch.object(Path, 'exists', return_value=False):
            self.assertEqual(mt.non_rewinding('/dev/st0'), '/dev/st0')

    def test_a_generic_node_is_left_alone(self):
        from apps.libraries.services.operations import mt
        self.assertEqual(mt.non_rewinding('/dev/sg6'), '/dev/sg6')

    def test_the_verification_harness_uses_the_same_rule(self):
        from apps.libraries.services.verification import tape_io
        from pathlib import Path
        with mock.patch.object(Path, 'exists', return_value=True):
            self.assertEqual(tape_io.writable_device('/dev/st0'), '/dev/nst0')


class SettingsIsolationTests(TestCase):
    """Running a command must not repoint the process at /etc/mhvtl.

    setup_django() used to overwrite settings.MHVTL_CONFIG_DIR unconditionally.
    In a real CLI process that is the point; inside the test runner it meant
    every test after the first CLI test read the live configuration. It was
    caught only because this account could not read /etc/mhvtl at the time -
    with read access it would have passed silently.
    """

    def test_a_command_leaves_the_configured_directory_alone(self):
        from django.conf import settings
        from apps.libraries.services.libraries import LibraryService

        before = settings.MHVTL_CONFIG_DIR
        with mock.patch.object(LibraryService, 'next_id',
                               return_value=success_result('n', {'library_id': 40})):
            run(['library', 'next-id'])
        self.assertEqual(settings.MHVTL_CONFIG_DIR, before)

    def test_config_dir_still_reaches_the_service(self):
        from apps.libraries.services.libraries import LibraryService
        with mock.patch.object(LibraryService, '__init__',
                               return_value=None) as init, \
             mock.patch.object(LibraryService, 'list', return_value=LIBRARIES):
            run(['--config-dir', '/srv/copy', 'library', 'list'])
        self.assertEqual(init.call_args[0][0], '/srv/copy')


class TapeCommandTests(TestCase):
    TAPES = success_result('2 tapes', {
        'library_id': 10, 'count': 2,
        'tapes': [{'barcode': 'E01001L8', 'slot': 1, 'kind': 'data',
                   'density': 'LTO8', 'used_mb': 0, 'capacity_mb': None,
                   'media_exists': True},
                  {'barcode': 'CLN101L8', 'slot': 2, 'kind': 'clean',
                   'density': 'LTO8', 'used_mb': None, 'capacity_mb': None,
                   'media_exists': True}],
        'summary': {'empty_slots': 37}})

    def test_list_prints_each_tape(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(TapeService, 'list', return_value=self.TAPES):
            code, out, err = run(['tape', 'list', '10'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('E01001L8', out)
        self.assertIn('37 empty slots', out)

    def test_an_unknown_capacity_prints_as_absent_not_zero(self):
        """On 1.8 an unloaded tape's capacity cannot be read; 0 would read as a
        full tape."""
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(TapeService, 'list', return_value=self.TAPES):
            code, out, err = run(['tape', 'list', '10'])
        row = next(line for line in out.splitlines() if 'E01001L8' in line)
        self.assertIn(output.EMPTY, row)

    def test_no_usage_skips_the_measurement(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(TapeService, 'list', return_value=self.TAPES) as listing:
            run(['tape', 'list', '10', '--no-usage'])
        self.assertFalse(listing.call_args[1]['with_usage'])

    def test_next_barcode_prints_only_the_barcode(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(TapeService, 'next_barcode',
                               return_value=success_result('n', {'barcode': 'E01033L8'})):
            code, out, err = run(['tape', 'next-barcode', '10'])
        self.assertEqual(out.strip(), 'E01033L8')

    def test_delete_keeps_the_media_unless_asked(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'delete',
                               return_value=success_result('removed')) as delete:
            code, out, err = run(['tape', 'delete', '10', 'E01001L8'])
        self.assertFalse(delete.call_args[1]['remove_media'])
        self.assertNotIn('cannot be undone', out)

    def test_removing_media_says_it_cannot_be_undone(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'delete',
                               return_value=success_result('removed')):
            code, out, err = run(['tape', 'delete', '10', 'E01001L8',
                                  '--remove-media'])
        self.assertIn('cannot be undone', out)

    def test_creating_needs_write_access(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(TapeService, 'create') as create:
            code, out, err = run(['tape', 'create', '10', 'E01040L8'])
        self.assertEqual(code, output.EXIT_DENIED)
        create.assert_not_called()

    def test_creating_passes_the_options_through(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'create',
                               return_value=success_result('created')) as create:
            code, out, err = run(['tape', 'create', '10', 'E01040L8',
                                  '--slot', '7', '--size-mb', '1000',
                                  '--density', 'LTO8', '--kind', 'WORM'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(create.call_args[0][:2], (10, 'E01040L8'))
        self.assertEqual(create.call_args[1],
                         {'slot': 7, 'size_mb': 1000, 'density': 'LTO8',
                          'kind': 'WORM'})

    def test_creating_defaults_the_capacity_and_reads_the_rest_from_the_barcode(self):
        """Omitted density and kind stay None: the service reads them from the
        barcode, which is the one place that mapping lives."""
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'create',
                               return_value=success_result('created')) as create:
            run(['tape', 'create', '10', 'E01040L8'])
        self.assertEqual(create.call_args[1]['size_mb'], 500000)
        self.assertIsNone(create.call_args[1]['density'])
        self.assertIsNone(create.call_args[1]['kind'])


class TapeBulkCommandTests(TestCase):
    """`tape bulk`: a run of tapes, and the numbers it starts from."""

    def bulk(self, argv, result=None):
        from apps.libraries.services.tapes import TapeService
        result = result or success_result('3 tapes created', {'created': 3})
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'create_bulk',
                               return_value=result) as create_bulk:
            code, out, err = run(argv)
        return code, out, err, create_bulk

    def test_it_creates_the_count_asked_for(self):
        code, out, err, create_bulk = self.bulk(['tape', 'bulk', '10', '3'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(create_bulk.call_args[0][:2], (10, 3))
        self.assertIn('3 tapes created', out)

    def test_the_series_can_be_given_or_left_to_the_library(self):
        _, _, _, create_bulk = self.bulk(['tape', 'bulk', '10', '5',
                                          '--prefix', 'E02', '--suffix', 'L8',
                                          '--start', '40'])
        self.assertEqual(create_bulk.call_args[1]['prefix'], 'E02')
        self.assertEqual(create_bulk.call_args[1]['suffix'], 'L8')
        self.assertEqual(create_bulk.call_args[1]['start_number'], 40)

        _, _, _, create_bulk = self.bulk(['tape', 'bulk', '10', '5'])
        self.assertIsNone(create_bulk.call_args[1]['prefix'])
        self.assertIsNone(create_bulk.call_args[1]['start_number'])

    def test_it_needs_write_access(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(TapeService, 'create_bulk') as create_bulk:
            code, out, err = run(['tape', 'bulk', '10', '3'])
        self.assertEqual(code, output.EXIT_DENIED)
        create_bulk.assert_not_called()

    def test_a_failed_run_is_a_failure_with_the_reason(self):
        code, out, err, _ = self.bulk(
            ['tape', 'bulk', '10', '3'],
            result=failure_result('no free slots', ['library 10 is full']))
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('no free slots', err)
        self.assertEqual(out, '')


class TapeMediaCommandTests(TestCase):
    """`tape media`: which cartridges this library's drives take."""

    MEDIA = {
        'drives': ['ULT3580-TD8', 'ULT3580-TD8'],
        'known': True,
        'default': 'LTO8',
        'media': [
            {'density': 'LTO8', 'suffix': 'L8', 'writable': True,
             'writable_in': ['ULT3580-TD8'], 'read_only_in': []},
            {'density': 'LTO6', 'suffix': 'L6', 'writable': False,
             'writable_in': [], 'read_only_in': ['ULT3580-TD8']},
        ]}

    def media(self, argv, info=None):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(TapeService, 'media_for_library',
                               return_value=info if info is not None else self.MEDIA):
            return run(argv)

    def test_it_prints_the_drives_and_a_row_per_density(self):
        code, out, err = self.media(['tape', 'media', '10'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('ULT3580-TD8', out)
        self.assertIn('LTO8', out)
        self.assertIn('LTO6', out)
        self.assertIn('Default for new tapes: LTO8', out)

    def test_read_only_media_says_so(self):
        """A tape a drive can only read restores but cannot be backed up to,
        so the difference has to be visible before one is created."""
        _, out, _ = self.media(['tape', 'media', '10'])
        row = next(line for line in out.splitlines() if 'LTO6' in line)
        self.assertIn('read-only', row)
        row = next(line for line in out.splitlines() if 'LTO8' in line)
        self.assertIn('read/write', row)

    def test_a_library_without_drives_is_a_failure(self):
        code, out, err = self.media(['tape', 'media', '10'],
                                    info={'drives': [], 'known': False,
                                          'default': None, 'media': []})
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('no drives', err)

    def test_unknown_drives_say_any_density_may_be_tried(self):
        code, out, err = self.media(
            ['tape', 'media', '10'],
            info={'drives': ['SOMETHING'], 'known': False, 'default': None,
                  'media': []})
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('any density', out)

    def test_json_is_the_service_dictionary(self):
        code, out, err = self.media(['--json', 'tape', 'media', '10'])
        self.assertEqual(json.loads(out), self.MEDIA)


class TapeSlotsCommandTests(TestCase):
    """`tape slots`: how many slots are used and free."""

    SUMMARY = {'total_slots': 39, 'full_slots': 32, 'empty_slots': 7,
               'map_slots': 4, 'drives': 4, 'cleaning_tapes': 1}

    def slots(self, argv, contents=True):
        from apps.libraries.services.config.service import ConfigService
        fake = mock.Mock() if contents else None
        if fake is not None:
            fake.summary.return_value = dict(self.SUMMARY)
        with mock.patch.object(ConfigService, 'library_contents',
                               return_value=fake):
            return run(argv)

    def test_it_prints_the_counts(self):
        code, out, err = self.slots(['tape', 'slots', '10'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('39', out)
        self.assertIn('empty slots', out.replace('_', ' '))

    def test_a_library_it_cannot_read_is_a_failure(self):
        code, out, err = self.slots(['tape', 'slots', '10'], contents=False)
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('library_contents.10', err)
        self.assertEqual(out, '')

    def test_json_carries_the_library_id_with_the_counts(self):
        code, out, err = self.slots(['--json', 'tape', 'slots', '10'])
        self.assertEqual(json.loads(out),
                         {'library_id': 10, **self.SUMMARY})


class DriveCommandTests(TestCase):
    def test_list_can_be_limited_to_a_library(self):
        from apps.libraries.services.drives import DriveService
        drives = success_result('1 drive', {'drives': [
            {'drive_id': 11, 'library_id': 10, 'slot': 1, 'vendor': 'IBM',
             'product': 'ULT3580-TD8', 'serial': 'XYZZY_A1', 'target': 1}]})
        with mock.patch.object(DriveService, 'list', return_value=drives) as listing:
            code, out, err = run(['drive', 'list', '10'])
        self.assertEqual(listing.call_args[0][0], 10)
        self.assertIn('ULT3580-TD8', out)

    def test_add_passes_only_the_overrides_given(self):
        from apps.libraries.services.drives import DriveService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(DriveService, 'add',
                               return_value=success_result('added')) as add:
            run(['drive', 'add', '10', '--serial', 'ABC123'])
        self.assertEqual(add.call_args[0], (10, {'serial': 'ABC123'}))

    def test_add_with_no_overrides_uses_the_profile(self):
        from apps.libraries.services.drives import DriveService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(DriveService, 'add',
                               return_value=success_result('added')) as add:
            run(['drive', 'add', '10'])
        self.assertIsNone(add.call_args[0][1])


class OperationCommandTests(TestCase):
    def _ops(self, name, result=None):
        from apps.libraries.services.operations.service import OperationsService
        return mock.patch.object(OperationsService, name,
                                 return_value=result or success_result('done'))

    def test_mount_takes_the_mtx_drive_number(self):
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             self._ops('mount') as mount:
            code, out, err = run(['op', 'mount', '10', '5', '0'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(mount.call_args[0], (10, 5, 0))

    def test_unmount_returns_to_its_own_slot_by_default(self):
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             self._ops('unmount') as unmount:
            run(['op', 'unmount', '10', '0'])
        self.assertIsNone(unmount.call_args[1]['slot'])

    def test_map_only_accepts_the_known_verbs(self):
        """Refused by argparse before anything reaches vtlcmd."""
        with self.assertRaises(SystemExit):
            run(['op', 'map', '10', 'delete'])

    def test_a_busy_drive_is_a_failure_with_the_reason(self):
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             self._ops('mount', failure_result('Drive 0 is occupied',
                                               ['holds E01003L8'])):
            code, out, err = run(['op', 'mount', '10', '5', '0'])
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('holds E01003L8', err)

    def test_every_operation_needs_write_access(self):
        with mock.patch.object(privileges, 'can_write', return_value=False):
            for argv in (['op', 'mount', '10', '1', '0'],
                         ['op', 'unmount', '10', '0'],
                         ['op', 'move', '10', '1', '2'],
                         ['op', 'online', '10'],
                         ['op', 'offline', '10'],
                         ['op', 'map', '10', 'open']):
                with self.subTest(argv=argv):
                    self.assertEqual(run(argv)[0], output.EXIT_DENIED)


class ServiceCommandTests(TestCase):
    def test_stopping_the_target_says_it_stops_everything(self):
        """"stop" reads narrower than it is."""
        from apps.libraries.services.console import units
        from apps.libraries.services.core.shell import CommandResult
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(units, 'stop',
                               return_value=CommandResult(['systemctl'], 0, '', '')):
            code, out, err = run(['service', 'stop'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('every library and drive', out)

    def test_one_library_uses_its_drive_ids_from_device_conf(self):
        from apps.libraries.services.config.service import ConfigService
        from apps.libraries.services.config import device_conf
        from apps.libraries.services.console import units
        from pathlib import Path

        conf = device_conf.parse(
            (Path(__file__).parent / 'fixtures' / 'device.conf').read_text())
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'device_conf', return_value=conf), \
             mock.patch.object(units, 'stop_library', return_value={}) as stop:
            run(['service', 'stop', '--library', '20'])
        self.assertEqual(stop.call_args[0], (20, [21, 22, 23, 24]))

    def test_an_unknown_library_is_refused(self):
        from apps.libraries.services.config.service import ConfigService
        from apps.libraries.services.config import device_conf
        from apps.libraries.services.console import units
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'device_conf',
                               return_value=device_conf.parse('')), \
             mock.patch.object(units, 'stop_library') as stop:
            code, out, err = run(['service', 'stop', '--library', '99'])
        self.assertEqual(code, output.EXIT_FAILED)
        stop.assert_not_called()


class ConsoleCommandTests(TestCase):
    def test_a_refused_log_path_is_a_failure(self):
        from apps.libraries.services.console import logs
        with mock.patch.object(logs, 'read',
                               return_value={'success': False,
                                             'error': 'not an allowed log'}):
            code, out, err = run(['console', 'logs', '/etc/shadow'])
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('not an allowed log', err)

    def test_log_lines_go_to_stdout(self):
        from apps.libraries.services.console import logs
        with mock.patch.object(logs, 'read',
                               return_value={'success': True,
                                             'lines': ['one', 'two']}):
            code, out, err = run(['console', 'logs', '-n', '2'])
        self.assertEqual(out.splitlines(), ['one', 'two'])


class ConfigCommandTests(TestCase):
    def test_show_refuses_a_file_outside_the_allowlist(self):
        from apps.libraries.services.config.service import ConfigService
        with mock.patch.object(ConfigService, 'read_file') as read:
            code, out, err = run(['config', 'show', '../../etc/shadow'])
        self.assertEqual(code, output.EXIT_USAGE)
        read.assert_not_called()

    def test_show_prints_the_file_verbatim(self):
        from apps.libraries.services.config.service import ConfigService
        with mock.patch.object(ConfigService, 'read_file',
                               return_value='VERSION: 5\n'):
            code, out, err = run(['config', 'show', 'device.conf'])
        self.assertEqual(out, 'VERSION: 5\n')

    def test_restore_only_accepts_a_directory_under_backups(self):
        """restore() copies everything it finds over the live configuration."""
        from apps.libraries.services.config.service import ConfigService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'restore') as restore:
            code, out, err = run(['config', 'restore', '/tmp'])
        self.assertEqual(code, output.EXIT_USAGE)
        restore.assert_not_called()

    def test_restore_resolves_a_bare_backup_name(self):
        import tempfile
        from pathlib import Path
        from apps.libraries.services.config.service import ConfigService

        base = Path(tempfile.mkdtemp())
        (base / 'backups' / '20260917_101500').mkdir(parents=True)
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'restore',
                               return_value=success_result('restored')) as restore:
            code, out, err = run(['--config-dir', str(base), 'config', 'restore',
                                  '20260917_101500'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(restore.call_args[0][0],
                         (base / 'backups' / '20260917_101500').resolve())


class ScsiCommandTests(TestCase):
    def test_a_declared_device_the_kernel_cannot_see_is_a_failure(self):
        from apps.libraries.services.scsi import mapping
        with mock.patch.object(mapping, 'map_all',
                               return_value={'libraries': {10: '/dev/sg5', 20: None},
                                             'drives': {11: '/dev/st3'}}):
            code, out, err = run(['scsi', 'map'])
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('not visible', out)


class IscsiCommandTests(TestCase):
    def test_status_says_which_source_it_read(self):
        """saveconfig.json is what was configured, not necessarily what is
        exported now."""
        from apps.libraries.services.iscsi import IscsiService
        status = success_result('ok', {
            'service': {'running': True, 'enabled': True},
            'targets': [{}], 'backstores': [{}, {}], 'lun_count': 2,
            'config_saved': True, 'source': 'saveconfig.json'})
        with mock.patch.object(IscsiService, 'status', return_value=status):
            code, out, err = run(['iscsi', 'status'])
        self.assertIn('saveconfig.json', out)

    def test_export_uses_the_generic_node_the_address_resolves_to(self):
        """By address, at the moment of export - never a remembered /dev/sgN."""
        from pathlib import Path
        from apps.libraries.services.config import device_conf
        from apps.libraries.services.config.service import ConfigService
        from apps.libraries.services.iscsi import IscsiService
        from apps.libraries.services.scsi import lsscsi, mapping
        from apps.libraries.services.scsi.models import ScsiAddress, ScsiDevice

        conf = device_conf.parse(
            (Path(__file__).parent / 'fixtures' / 'device.conf').read_text())
        tape = ScsiDevice(address=ScsiAddress(16, 0, 1, 0), device_type='tape',
                          vendor='IBM', model='ULT3580-TD8', revision='0108',
                          device_path='/dev/st3', generic_path='/dev/sg8')
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'device_conf', return_value=conf), \
             mock.patch.object(mapping, 'map_all',
                               return_value={'libraries': {10: '/dev/sg5'},
                                             'drives': {11: '/dev/st3'}}), \
             mock.patch.object(lsscsi, 'discover', return_value=[tape]), \
             mock.patch.object(IscsiService, 'export_library',
                               return_value=success_result('exported')) as export:
            code, out, err = run(['iscsi', 'export', '10'])

        devices = export.call_args[0][1]
        self.assertEqual(devices[0], {'name': 'lib10_changer',
                                      'device_path': '/dev/sg5', 'type': 'changer'})
        self.assertEqual(devices[1]['device_path'], '/dev/sg8')
        self.assertTrue(export.call_args[1]['allow_all_initiators'])

    def test_naming_an_initiator_closes_the_target(self):
        from apps.libraries.services.config import device_conf
        from apps.libraries.services.config.service import ConfigService
        from apps.libraries.services.iscsi import IscsiService
        from apps.libraries.services.scsi import mapping

        conf = device_conf.parse('Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00\n')
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'device_conf', return_value=conf), \
             mock.patch.object(mapping, 'map_all',
                               return_value={'libraries': {10: '/dev/sg5'},
                                             'drives': {}}), \
             mock.patch.object(IscsiService, 'export_library',
                               return_value=success_result('exported')) as export:
            run(['iscsi', 'export', '10', '--initiator',
                 'iqn.2026-01.com.example:backup01'])
        self.assertFalse(export.call_args[1]['allow_all_initiators'])
        self.assertEqual(export.call_args[1]['initiator_iqn'],
                         'iqn.2026-01.com.example:backup01')


class IscsiRemapCommandTests(TestCase):
    def test_a_dry_run_needs_no_write_access(self):
        from apps.libraries.services.iscsi import remap
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(remap, 'remap',
                               return_value=success_result('0 would change',
                                                           {'changes': []})):
            code, out, err = run(['iscsi', 'remap', '--dry-run'])
        self.assertEqual(code, output.EXIT_OK)

    def test_apply_restarts_the_target_only_after_a_successful_remap(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.core.shell import CommandResult
        from apps.libraries.services.iscsi import remap

        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(remap, 'remap',
                               return_value=failure_result('bad json', ['x'])), \
             mock.patch.object(units, 'restart') as restart:
            code, out, err = run(['iscsi', 'remap', '--apply'])
        self.assertEqual(code, output.EXIT_FAILED)
        restart.assert_not_called()

        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(remap, 'remap',
                               return_value=success_result('repointed 1',
                                                           {'changes': []})), \
             mock.patch.object(units, 'restart',
                               return_value=CommandResult(['systemctl'], 0, '', '')) as restart:
            code, out, err = run(['iscsi', 'remap', '--apply'])
        self.assertEqual(code, output.EXIT_OK)
        restart.assert_called_once_with('target.service')


class LibraryPreviewCommandTests(TestCase):
    """`library create --dry-run`: what it prints, where, and its exit code.

    A preview reports what create() would say about the specification. An
    error is a refusal, a warning is not, and printing both as "warning" with
    exit 1 for either made a creatable library look rejected.
    """

    def preview(self, *, errors=(), warnings=(), json_output=False):
        from apps.libraries.services.libraries import lifecycle

        result = success_result('Preview of library 40', {
            'text': 'Library: 40 CHANNEL: 00 TARGET: 18 LUN: 00\n',
            'library_id': 40, 'drive_ids': [41],
            'errors': list(errors), 'warnings': list(warnings)})
        argv = ['library', 'create', '--profile', 'IBM', '--dry-run']
        with mock.patch.object(lifecycle, 'preview', return_value=result), \
             mock.patch.object(privileges, 'can_write', return_value=False):
            return run((['--json'] if json_output else []) + argv)

    def test_a_clean_preview_prints_the_text_and_succeeds(self):
        code, out, err = self.preview()
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('Library: 40', out)
        self.assertEqual(err, '')

    def test_a_warning_is_not_a_failure(self):
        code, out, err = self.preview(
            warnings=["Media type 'LTO3' is read-only in 'ULT3580-TD5'"])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('warning: ', err)
        self.assertIn('read-only', err)

    def test_an_error_is_a_failure(self):
        code, out, err = self.preview(
            errors=["Drive model 'NOPE' is not valid for IBM"])
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn("error: Drive model 'NOPE'", err)

    def test_the_problems_stay_off_stdout(self):
        """A redirected preview has to remain a usable device.conf."""
        code, out, err = self.preview(errors=['bad'], warnings=['odd'])
        self.assertNotIn('bad', out)
        self.assertNotIn('odd', out)
        self.assertIn('bad', err)
        self.assertIn('odd', err)

    def test_json_carries_both_lists_and_the_exit_code(self):
        code, out, err = self.preview(errors=['bad'], warnings=['odd'],
                                      json_output=True)
        self.assertEqual(code, output.EXIT_FAILED)
        payload = json.loads(out)['data']
        self.assertEqual(payload['errors'], ['bad'])
        self.assertEqual(payload['warnings'], ['odd'])


class LibrarySlotsCommandTests(TestCase):
    """`library slots`: read the counts, or change how many are empty."""

    SUMMARY = {'total_slots': 6, 'full_slots': 2, 'empty_slots': 4,
               'map_slots': 4, 'drives': 2, 'cleaning_tapes': 0}

    def contents(self):
        fake = mock.Mock()
        fake.summary.return_value = dict(self.SUMMARY)
        return fake

    def test_with_no_option_it_only_reads(self):
        from apps.libraries.services.config.service import ConfigService
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(ConfigService, 'library_contents',
                               return_value=self.contents()), \
             mock.patch.object(lifecycle, 'set_empty_slots') as change:
            code, out, err = run(['library', 'slots', '40'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('6', out)
        change.assert_not_called()

    def test_empty_sets_the_number(self):
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(
                 lifecycle, 'set_empty_slots',
                 return_value=success_result('Library 40 now has 10 slots',
                                             {'restart_required': True})) as change:
            code, out, err = run(['library', 'slots', '40', '--empty', '6'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(change.call_args[0][:2], (40, 6))

    def test_add_is_relative_to_what_is_there(self):
        from apps.libraries.services.config.service import ConfigService
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(ConfigService, 'library_contents',
                               return_value=self.contents()), \
             mock.patch.object(
                 lifecycle, 'set_empty_slots',
                 return_value=success_result('ok', {})) as change:
            run(['library', 'slots', '40', '--add', '2'])
        self.assertEqual(change.call_args[0][1], 6)   # 4 there + 2

    def test_it_says_the_library_must_be_restarted(self):
        """The daemon reads library_contents once, at start."""
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(lifecycle, 'set_empty_slots',
                               return_value=success_result('ok', {})):
            code, out, err = run(['library', 'slots', '40', '--empty', '6'])
        self.assertIn('service restart --library 40', out)

    def test_restart_does_it_instead_of_saying_it(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(lifecycle, 'set_empty_slots',
                               return_value=success_result('ok', {})), \
             mock.patch.object(units, 'restart_library',
                               return_value={'ok': True}) as restart:
            code, out, err = run(['library', 'slots', '40', '--empty', '6',
                                  '--restart'])
        self.assertEqual(code, output.EXIT_OK)
        restart.assert_called_once_with(40)
        self.assertIn('Restarted vtllibrary@40', out)

    def test_a_failed_restart_is_reported_with_the_command_to_run(self):
        from apps.libraries.services.console import units
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(lifecycle, 'set_empty_slots',
                               return_value=success_result('ok', {})), \
             mock.patch.object(units, 'restart_library',
                               return_value={'ok': False, 'error': 'no'}):
            code, out, err = run(['library', 'slots', '40', '--empty', '6',
                                  '--restart'])
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertIn('service restart --library 40', err)

    def test_changing_needs_write_access(self):
        from apps.libraries.services.libraries import lifecycle
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(lifecycle, 'set_empty_slots') as change:
            code, out, err = run(['library', 'slots', '40', '--empty', '6'])
        self.assertEqual(code, output.EXIT_DENIED)
        change.assert_not_called()


class TapeAdoptCommandTests(TestCase):
    """`tape adopt`: an orphaned tape back into a library, with its data."""

    def test_it_adopts_and_says_a_restart_is_needed(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(
                 TapeService, 'adopt',
                 return_value=success_result('E01099L8 is now in slot 33',
                                             {'restart_required': True})) as adopt:
            code, out, err = run(['tape', 'adopt', '10', 'E01099L8'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertEqual(adopt.call_args[0][:2], (10, 'E01099L8'))
        self.assertIsNone(adopt.call_args[1]['slot'])
        self.assertIn('service restart --library 10', out)

    def test_the_slot_can_be_chosen(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'adopt',
                               return_value=success_result('ok')) as adopt:
            run(['tape', 'adopt', '10', 'E01099L8', '--slot', '33'])
        self.assertEqual(adopt.call_args[1]['slot'], 33)

    def test_it_needs_write_access(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=False), \
             mock.patch.object(TapeService, 'adopt') as adopt:
            code, out, err = run(['tape', 'adopt', '10', 'E01099L8'])
        self.assertEqual(code, output.EXIT_DENIED)
        adopt.assert_not_called()

    def test_a_failure_says_nothing_about_restarting(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'adopt',
                               return_value=failure_result('no media files')):
            code, out, err = run(['tape', 'adopt', '10', 'E01099L8'])
        self.assertEqual(code, output.EXIT_FAILED)
        self.assertNotIn('restart', out)

    def test_delete_points_at_adopt_when_the_media_is_kept(self):
        from apps.libraries.services.tapes import TapeService
        with mock.patch.object(privileges, 'can_write', return_value=True), \
             mock.patch.object(TapeService, 'delete',
                               return_value=success_result(
                                   'removed', {'barcode': 'E01001L8'})):
            code, out, err = run(['tape', 'delete', '10', 'E01001L8'])
        self.assertIn('tape adopt', out)


class OrphanMediaPrintingTests(TestCase):
    """Tapes on disk no library lists are printed apart from the rest."""

    REPORT = {'orphaned_drives': [], 'orphaned_files': [], 'orphaned_services': [],
              'orphaned_media': [{'barcode': 'E01099L8',
                                  'path': '/opt/mhvtl/E01099L8',
                                  'reason': 'no library_contents lists this barcode'}],
              'valid_library_ids': [10], 'valid_drive_ids': [], 'unreadable': []}

    def test_they_are_listed_with_the_command_to_put_one_back(self):
        from apps.libraries.services.libraries import orphans
        with mock.patch.object(orphans, 'find_result',
                               return_value=success_result('1 tape', self.REPORT)):
            code, out, err = run(['library', 'orphans'])
        self.assertEqual(code, output.EXIT_OK)
        self.assertIn('E01099L8', out)
        self.assertIn('data kept', out)
        self.assertIn('mhvtl tape adopt', out)
        self.assertNotIn('Nothing is orphaned', out)

    def test_nothing_at_all_still_says_so(self):
        from apps.libraries.services.libraries import orphans
        empty = {**self.REPORT, 'orphaned_media': []}
        with mock.patch.object(orphans, 'find_result',
                               return_value=success_result('0', empty)):
            code, out, err = run(['library', 'orphans'])
        self.assertIn('Nothing is orphaned', out)
