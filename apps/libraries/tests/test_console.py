"""Console: units, modules, logs, disk.

Step 7 of the service-layer refactor. systemctl, lsmod and df are faked: these
tests are about how their output is read, and a suite that depends on the host's
own service state fails for reasons that have nothing to do with the code.
"""
from unittest import mock

from django.test import TestCase

from apps.libraries.services.console import logs, modules, system, units
from apps.libraries.services.core.shell import CommandResult

LIST_UNITS_LIBRARIES = """\
vtllibrary@10.service loaded active running Robot Library Daemon
vtllibrary@20.service loaded active running Robot Library Daemon
vtllibrary@40.service loaded inactive dead Robot Library Daemon
"""

LIST_UNITS_DRIVES = """\
vtltape@11.service loaded active running Tape Daemon
vtltape@12.service loaded active running Tape Daemon
"""

DEVICE_CONF = """\
Library: 10 CHANNEL: 00 TARGET: 00 LUN: 00
 Vendor identification: STK

Drive: 11 CHANNEL: 00 TARGET: 01 LUN: 00
 Library ID: 10 Slot: 01

Drive: 12 CHANNEL: 00 TARGET: 02 LUN: 00
 Library ID: 10 Slot: 02

Library: 20 CHANNEL: 00 TARGET: 13 LUN: 00
 Vendor identification: SONY
"""


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


def failed(stderr='no'):
    return CommandResult(['fake'], 1, '', stderr)


class UnitStatusTests(TestCase):
    def _run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        if argv[:2] == ['systemctl', 'is-active']:
            return ok('active')
        if argv[:2] == ['systemctl', 'is-enabled']:
            return ok('enabled')
        if 'list-units' in argv:
            pattern = argv[-1]
            return ok(LIST_UNITS_LIBRARIES if 'vtllibrary' in pattern
                      else LIST_UNITS_DRIVES)
        return ok()

    def _status(self, config=True):
        with mock.patch.object(units.shell, 'run', side_effect=self._run), \
             mock.patch.object(units.shell, 'sudo_cat',
                               return_value=ok(DEVICE_CONF) if config else failed()), \
             mock.patch.object(modules, 'mhvtl_loaded', return_value=True):
            return units.status('/anywhere')

    def test_reads_the_target_state(self):
        status = self._status()
        self.assertTrue(status.target_active)
        self.assertTrue(status.target_enabled)

    def test_counts_running_daemons(self):
        status = self._status()
        self.assertEqual((status.libraries_active, len(status.libraries)), (2, 2))
        self.assertEqual((status.drives_active, len(status.drives)), (2, 2))

    def test_names_units_device_conf_no_longer_declares(self):
        """A deleted library leaves its unit loaded until the machine reboots.

        Counting it as a library that will not start reports a healthy system as
        broken, for ever.
        """
        status = self._status()
        self.assertEqual(status.stale, ['vtllibrary@40.service'])
        self.assertTrue(status.healthy)

    def test_without_device_conf_nothing_is_called_stale(self):
        """Better to count an extra unit than to call a real one stale."""
        status = self._status(config=False)
        self.assertEqual(status.stale, [])
        self.assertEqual(len(status.libraries), 3)

    def test_health_needs_the_module_as_well_as_the_target(self):
        with mock.patch.object(units.shell, 'run', side_effect=self._run), \
             mock.patch.object(units.shell, 'sudo_cat', return_value=ok(DEVICE_CONF)), \
             mock.patch.object(modules, 'mhvtl_loaded', return_value=False):
            self.assertFalse(units.status('/anywhere').healthy)


class ModuleStateTests(TestCase):
    def _with(self, loaded_names, builtin=''):
        lsmod = 'Module Size Used\n' + '\n'.join(f'{n} 1000 0' for n in loaded_names)

        def run(argv, **kwargs):
            argv = [str(a) for a in argv]
            if argv[0] == 'lsmod':
                return ok(lsmod)
            if argv[0] == 'uname':
                return ok('5.14.0')
            if argv[0] == 'cat':
                return ok(builtin)
            return ok()
        return mock.patch.object(modules.shell, 'run', side_effect=run)

    def test_sees_a_loaded_module(self):
        with self._with(['mhvtl', 'sg']):
            self.assertTrue(modules.mhvtl_loaded())
            self.assertEqual(modules.summary()['backend'], 'mhvtl')

    def test_sees_a_builtin_module(self):
        """lsmod never lists a built-in module; that is not the same as absent."""
        with self._with([], builtin='kernel/drivers/scsi/mhvtl.ko\n'):
            self.assertTrue(modules.mhvtl_loaded())

    def test_accepts_the_tcmu_backend(self):
        """1.8 can run on TCMU instead of mhvtl.ko; insisting on the module
        would report a working installation as broken."""
        with self._with(['target_core_user', 'tcm_loop']):
            self.assertTrue(modules.mhvtl_loaded())
            self.assertEqual(modules.summary()['backend'], 'tcmu')

    def test_reports_nothing_loaded(self):
        with self._with(['ext4']):
            self.assertFalse(modules.mhvtl_loaded())
            self.assertEqual(modules.summary()['backend'], 'none')


class LogReadingTests(TestCase):
    """The log path used to be interpolated into a shell=True command."""

    def test_refuses_a_path_outside_the_allowlist(self):
        with mock.patch.object(logs.shell, 'sudo') as sudo:
            result = logs.read('/etc/shadow')
        self.assertFalse(result['success'])
        sudo.assert_not_called()

    def test_refuses_a_shell_fragment(self):
        with mock.patch.object(logs.shell, 'sudo') as sudo:
            result = logs.read('/var/log/messages; cat /etc/shadow')
        self.assertFalse(result['success'])
        sudo.assert_not_called()

    def test_reads_an_allowlisted_log(self):
        with mock.patch.object(logs.shell, 'sudo', return_value=ok('one\ntwo\n')) as sudo:
            result = logs.read('/var/log/messages', lines=2)
        self.assertTrue(result['success'])
        self.assertEqual(result['lines'], ['one', 'two'])
        self.assertEqual(sudo.call_args[0][0],
                         ['tail', '-n', '2', '/var/log/messages'])

    def test_line_count_is_bounded(self):
        with mock.patch.object(logs.shell, 'sudo', return_value=ok('')) as sudo:
            logs.read('/var/log/messages', lines=10 ** 9)
        self.assertEqual(sudo.call_args[0][0][2], str(logs.MAX_LINES))

    def test_an_unreadable_log_is_reported_not_raised(self):
        with mock.patch.object(logs.shell, 'sudo', return_value=failed('denied')):
            result = logs.read('/var/log/messages')
        self.assertFalse(result['success'])
        self.assertIn('denied', result['error'])


class LibraryUnitControlTests(TestCase):
    """Enabling, starting and stopping one library's units.

    Moved out of mhvtl_library_service.py in step 8, where creating a library
    enabled and started each unit inline and deleting one assumed the drive ids
    were library_id+1..n.
    """

    def _capture(self):
        calls = []

        def control(action, unit=units.TARGET):
            calls.append((action, unit))
            return ok()
        return calls, mock.patch.object(units, '_control', side_effect=control)

    def test_units_are_named_from_the_drive_ids_not_a_range(self):
        """A library whose drives were added and removed has gaps, and guessing
        the range leaves the real units running after a delete."""
        self.assertEqual(units.library_units(10, [11, 14]),
                         ['vtllibrary@10.service', 'vtltape@11.service',
                          'vtltape@14.service'])

    def test_starting_enables_before_it_starts(self):
        calls, patch = self._capture()
        with patch, mock.patch.object(units, 'daemon_reload', return_value=ok()):
            units.start_library(10, [11])

        self.assertEqual(calls, [('enable', 'vtllibrary@10.service'),
                                 ('start', 'vtllibrary@10.service'),
                                 ('enable', 'vtltape@11.service'),
                                 ('start', 'vtltape@11.service')])

    def test_stopping_stops_before_it_disables(self):
        calls, patch = self._capture()
        with patch, mock.patch.object(units, 'daemon_reload', return_value=ok()):
            units.stop_library(10, [11])

        self.assertEqual([action for action, _ in calls],
                         ['stop', 'disable', 'stop', 'disable'])

    def test_starting_reports_which_units_failed(self):
        def control(action, unit=units.TARGET):
            return failed() if (action == 'start' and 'vtltape' in unit) else ok()

        with mock.patch.object(units, '_control', side_effect=control), \
             mock.patch.object(units, 'daemon_reload', return_value=ok()):
            outcome = units.start_library(10, [11])

        self.assertTrue(outcome['vtllibrary@10.service'])
        self.assertFalse(outcome['vtltape@11.service'])


class RestartTests(TestCase):
    def test_restarting_one_library_restarts_its_robot(self):
        with mock.patch.object(units, '_control', return_value=ok()) as control, \
             mock.patch.object(units, 'daemon_reload', return_value=ok()):
            outcome = units.restart_library(10)

        self.assertEqual(outcome['restarted'], 'vtllibrary@10.service')
        self.assertTrue(outcome['ok'])
        control.assert_called_once_with('restart', 'vtllibrary@10.service')

    def test_a_library_that_will_not_restart_leaves_the_others_alone(self):
        """One library failing used to restart mhvtl.target - every library -
        which cut off jobs on the others and broke every iSCSI export."""
        with mock.patch.object(units, '_control',
                               return_value=failed('no such unit')) as control, \
             mock.patch.object(units, 'daemon_reload', return_value=ok()):
            outcome = units.restart_library(10)

        control.assert_called_once_with('restart', 'vtllibrary@10.service')
        self.assertEqual(outcome['restarted'], 'vtllibrary@10.service')
        self.assertFalse(outcome['ok'])
        self.assertIn('no such unit', outcome['error'])

    def test_no_library_restarts_the_whole_target(self):
        with mock.patch.object(units, '_control', return_value=ok()) as control, \
             mock.patch.object(units, 'daemon_reload', return_value=ok()):
            self.assertEqual(units.restart_library()['restarted'], units.TARGET)
        control.assert_called_once_with('restart', units.TARGET)

    def test_a_failure_is_reported_with_its_reason(self):
        with mock.patch.object(units, '_control',
                               return_value=failed('unit is masked')), \
             mock.patch.object(units, 'daemon_reload', return_value=ok()):
            outcome = units.restart_library(10)

        self.assertFalse(outcome['ok'])
        self.assertIn('masked', outcome['error'])
        self.assertNotIn('fell_back_from', outcome)


class ActiveStateTests(TestCase):
    """`systemctl is-active` exits non-zero for an inactive unit, so the exit
    code says nothing useful; the word it prints is the answer."""

    def test_reads_the_word_not_the_exit_code(self):
        result = CommandResult(['systemctl'], 3, 'inactive\n', '')
        with mock.patch.object(units.shell, 'run', return_value=result):
            self.assertFalse(units.is_active('vtllibrary@10.service'))

    def test_an_active_unit(self):
        with mock.patch.object(units.shell, 'run', return_value=ok('active\n')):
            self.assertTrue(units.is_active('vtllibrary@10.service'))

    def test_enabled_is_read_the_same_way(self):
        with mock.patch.object(units.shell, 'run', return_value=ok('enabled\n')):
            self.assertTrue(units.is_enabled('vtllibrary@10.service'))


class MemoryPercentTests(TestCase):
    """The console dashboard's memory tile, which used to read a field
    SystemInfo did not have."""

    def test_in_use_is_total_less_available(self):
        from apps.libraries.services.console.system import memory_percent
        text = 'MemTotal:       16000000 kB\nMemFree:  1 kB\nMemAvailable:    4000000 kB\n'
        self.assertEqual(memory_percent(text), 75.0)

    def test_missing_fields_give_zero(self):
        from apps.libraries.services.console.system import memory_percent
        self.assertEqual(memory_percent('MemTotal: 100 kB\n'), 0.0)
        self.assertEqual(memory_percent(''), 0.0)


class MemoryLevelTests(TestCase):
    """How alarming the memory tile and bar look, decided by the system
    service. The dashboard template used to decide it, twice, with
    `> 80` / `> 60`; these pin that the move changed nothing."""

    def test_the_boundaries_are_the_templates_old_ones(self):
        from apps.libraries.services.console.system import memory_level
        for percent, level in ((0.0, 'normal'), (60.0, 'normal'), (60.1, 'warning'),
                               (80.0, 'warning'), (80.1, 'danger'), (100.0, 'danger')):
            with self.subTest(percent=percent):
                self.assertEqual(memory_level(percent), level)

    def test_the_command_line_gets_it_too(self):
        """`mhvtl console system` prints to_dict(), so it carries the level."""
        info = system.SystemInfo(memory_percent=69.3)
        self.assertEqual(info.to_dict()['memory_level'], 'warning')

    def test_the_refresh_endpoint_carries_it(self):
        from django.test import RequestFactory

        from apps.libraries import console_views
        request = RequestFactory().get('/libraries/ajax/console-refresh/')
        request.session = {'mhvtl_logged_in': True}
        summary = {'system_info': system.SystemInfo(memory_percent=85.0),
                   'mhvtl_service': {key: 0 for key in (
                       'target_active', 'target_enabled', 'modules_service_active',
                       'library_services_count', 'library_services_active',
                       'tape_services_count', 'tape_services_active',
                       'all_services_healthy', 'total_services', 'active_services')},
                   'kernel_modules': [], 'scsi_devices': []}
        with mock.patch.object(console_views, '_summary', return_value=summary):
            import json
            data = json.loads(console_views.console_refresh_ajax(request).content)
        self.assertEqual(data['data']['system_info']['memory_level'], 'danger')


class ConsolePagesTests(TestCase):
    """The console views call services/console directly.

    The adapter they used to go through is gone; these keep the names the
    templates and their JavaScript read.
    """

    def test_the_status_shape(self):
        from apps.libraries import console_views
        from apps.libraries.services.console.units import MhvtlServiceStatus, UnitState

        state = MhvtlServiceStatus(
            target_active=True, target_enabled=True, modules_loaded=True,
            libraries=[UnitState(name='vtllibrary@10.service', active=True)],
            drives=[UnitState(name='vtltape@11.service', active=True),
                    UnitState(name='vtltape@12.service', active=False)])
        with mock.patch.object(console_views.units, 'status', return_value=state):
            shaped = console_views._service_status()
        self.assertEqual(shaped['library_services_count'], 1)
        self.assertEqual(shaped['tape_services_active'], 1)
        self.assertEqual((shaped['active_services'], shaped['total_services']), (2, 3))
        self.assertFalse(shaped['all_services_healthy'])

    def test_no_view_module_imports_the_console_adapter(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for path in root.rglob('*.py'):
            if 'tests' in path.parts:
                continue
            with self.subTest(path=path.name):
                self.assertNotIn('adapters.console_service', path.read_text())


class ForgetLibraryTests(TestCase):

    def test_reloads_then_clears_each_unit(self):
        calls = []
        with mock.patch.object(units, 'daemon_reload', side_effect=lambda: calls.append('reload')), \
             mock.patch.object(units, 'reset_failed', side_effect=lambda u: calls.append(u)):
            units.forget_library(40, [41, 42])
        self.assertEqual(calls, ['reload', 'vtllibrary@40.service',
                                 'vtltape@41.service', 'vtltape@42.service'])


class CpuFactsTests(TestCase):
    """The console's CPU row: /proc/cpuinfo, not a guess.

    The template asked for cpu_model and cpu_count from the start and the
    service had neither, so every host read "CPU Unknown ( cores)".
    """
    INTEL = ("processor\t: 0\nvendor_id\t: GenuineIntel\n"
             "model name\t: Intel(R) Core(TM) i7-7700HQ CPU @ 2.80GHz\n"
             "\nprocessor\t: 1\nmodel name\t: Intel(R) Core(TM) i7-7700HQ CPU @ 2.80GHz\n")

    def test_it_reads_the_model_and_counts_the_processors(self):
        model, count = system.cpu(self.INTEL)
        self.assertEqual(model, 'Intel(R) Core(TM) i7-7700HQ CPU @ 2.80GHz')
        self.assertEqual(count, 2)

    def test_a_kernel_without_a_model_name_still_counts(self):
        """ARM gives no `model name`; a blank model beats a wrong one."""
        model, count = system.cpu('processor\t: 0\n\nprocessor\t: 1\n')
        self.assertEqual(model, '')
        self.assertEqual(count, 2)

    def test_nothing_readable_is_blank_and_zero(self):
        self.assertEqual(system.cpu(''), ('', 0))

    def test_the_facts_reach_the_dictionary_the_page_reads(self):
        info = system.SystemInfo(cpu_model='Some CPU', cpu_count=4)
        self.assertEqual(info.to_dict()['cpu_model'], 'Some CPU')
        self.assertEqual(info.to_dict()['cpu_count'], 4)


class ConsoleDashboardPageTests(TestCase):
    """The console page against what the services really return.

    Nothing rendered this template before, so it asked for names no service
    had - os_name, kernel_version, cpu_model, used_mb, kernel_modules.modules,
    device.host - and every one of them showed as "Unknown", "MB / MB (%)",
    an empty card or "[:::]" on a working host.
    """

    def _page(self):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.libraries import console_views
        from apps.libraries.services.console import disk
        from apps.libraries.services.scsi.models import ScsiAddress, ScsiDevice

        summary = {
            'system_info': system.SystemInfo(
                hostname='mhvtl', kernel='5.14.0-687', distribution='Rocky Linux 9.8',
                cpu_model='Intel(R) Core(TM) i7-7700HQ CPU @ 2.80GHz', cpu_count=2,
                uptime='up 7 hours', load_average='0.70 0.69 0.65',
                memory_total='3.6Gi', memory_used='2.5Gi', memory_percent=69.3),
            'mhvtl_service': {'target_active': True, 'target_enabled': True,
                              'modules_service_active': True,
                              'library_services_count': 3, 'library_services_active': 3,
                              'tape_services_count': 12, 'tape_services_active': 12,
                              'all_services_healthy': True, 'total_services': 15,
                              'active_services': 15},
            'kernel_modules': [{'name': 'mhvtl', 'loaded': True, 'required': True},
                               {'name': 'tcm_loop', 'loaded': False, 'required': False}],
            'disk_usage': [disk.DiskUsage(path='/opt/mhvtl', filesystem='/dev/sda2',
                                          size='62G', used='31G', available='32G',
                                          percent=50)],
            'mhvtl_data': {'data_dir': '/opt/mhvtl', 'config_dir': '/etc/mhvtl',
                           'data_size': '1.7M', 'tape_count': 97, 'details': []},
            'scsi_devices': [ScsiDevice(address=ScsiAddress(16, 0, 0, 0),
                                        device_type='mediumx', vendor='STK',
                                        model='L700', revision='0108',
                                        device_path='', generic_path='/dev/sg6')],
        }
        request = RequestFactory().get('/libraries/console/')
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        with mock.patch.object(console_views, '_summary', return_value=summary):
            return console_views.ConsoleDashboardView().get(request).content.decode()

    def test_the_host_facts_are_shown_not_unknown(self):
        body = self._page()
        for value in ('Rocky Linux 9.8', '5.14.0-687', 'i7-7700HQ', '(2 cores)',
                      '2.5Gi / 3.6Gi'):
            self.assertIn(value, body)
        self.assertNotIn('Unknown', body)

    def test_the_memory_card_and_bar_use_the_services_level(self):
        """69.3% is a warning; the template prints the word, it decides nothing."""
        body = self._page()
        self.assertIn('stat-card warning"', body)
        self.assertIn('progress-fill warning"', body)
        self.assertNotIn('memory_percent >', body)

    def test_disk_usage_shows_the_sizes_df_reported(self):
        body = self._page()
        self.assertIn('31G / 62G (50%)', body)
        self.assertNotIn('MB / MB', body)

    def test_the_kernel_modules_card_lists_the_modules(self):
        body = self._page()
        self.assertIn('mhvtl', body)
        self.assertIn('tcm_loop', body)
        self.assertIn('Not Loaded', body)

    def test_a_scsi_device_shows_its_address(self):
        body = self._page()
        self.assertIn('[16:0:0:0]', body)
        self.assertNotIn('[:::]', body)

    def test_the_mhvtl_data_box_follows_the_theme(self):
        """It was a hard-coded navy; in Sepia and Daylight it stayed dark."""
        body = self._page()
        self.assertIn('1.7M', body)
        self.assertIn('var(--surface-deep)', body)
        self.assertNotIn('#111827', body)


class ConsoleFrameTests(TestCase):
    """Every console page uses the one frame, and links to every other.

    They each carried their own <head>, header and navigation, and no two
    navigations agreed: the console dashboard offered Logs, Service and
    Devices, the service page offered Console and Logs, and neither the
    kernel modules nor the disk usage was linked from anywhere - both were
    reachable only by typing the address.
    """

    PAGES = {
        'dashboard.html': {'title': 'System Console', 'system_info': {},
                           'mhvtl_service': {}, 'scsi_devices': []},
        'logs.html': {'title': 'Logs', 'log_data': {}, 'available_types': []},
        'service_status.html': {'title': 'Services', 'service': {}},
        'kernel_modules.html': {'title': 'Modules', 'modules': []},
        'disk_usage.html': {'title': 'Disk', 'disk_usage': []},
        'scsi_devices.html': {'title': 'Devices', 'devices': [], 'device_count': 0},
    }

    def _render(self, page):
        from django.template.loader import render_to_string
        return render_to_string('libraries/console/' + page, self.PAGES[page])

    def test_every_page_is_themed(self):
        """The shared rules are scoped to body.mhvtl-console, which only a
        base template sets - a page without it cannot follow the theme."""
        for page in self.PAGES:
            with self.subTest(page=page):
                html = self._render(page)
                self.assertIn('class="mhvtl-console"', html)
                self.assertIn('css/mhvtl-console.css', html)
                self.assertIn('theme-picker', html)

    def test_every_page_links_to_every_other(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                html = self._render(page)
                for link in ('/libraries/console/', '/libraries/console/logs/',
                             '/libraries/console/service/',
                             '/libraries/console/devices/',
                             '/libraries/console/modules/',
                             '/libraries/console/disk/'):
                    self.assertIn('href="%s"' % link, html)

    def test_no_page_carries_its_own_head(self):
        from pathlib import Path
        folder = (Path(__file__).resolve().parents[1]
                  / 'templates/libraries/console')
        for page in self.PAGES:
            with self.subTest(page=page):
                text = (folder / page).read_text()
                self.assertIn("{% extends 'libraries/console/base.html' %}", text)
                self.assertNotIn('<!DOCTYPE', text)

    def test_no_page_hard_codes_a_colour(self):
        """A colour written into the page ignores the theme: grey text on the
        Daylight background is what that looks like."""
        import re
        from pathlib import Path
        folder = (Path(__file__).resolve().parents[1]
                  / 'templates/libraries/console')
        for page in self.PAGES:
            with self.subTest(page=page):
                text = (folder / page).read_text()
                self.assertEqual(re.findall(r'(?<!&)#[0-9a-fA-F]{3,6}\b', text), [])


class ScsiDevicePageTests(TestCase):
    """The device table prints what the service actually returns.

    Every row read "[:::]" with an empty type badge: the template asked for
    device.host, .channel, .target, .lun and .type, and ScsiDevice carries
    one address object and device_type. A template renders a missing
    attribute as nothing, so the page looked plausible and raised nothing.
    """

    def _devices(self):
        from apps.libraries.services.scsi.lsscsi import parse_line
        return [parse_line('[16:0:0:0]   mediumx STK      L700             '
                           '0108  -          /dev/sg6'),
                parse_line('[16:0:1:0]   tape    IBM      ULT3580-TD8      '
                           '0108  /dev/st2   /dev/sg7')]

    def _render(self, devices=None, show_all=False):
        from django.template.loader import render_to_string
        devices = self._devices() if devices is None else devices
        return render_to_string('libraries/console/scsi_devices.html',
                                {'title': 'SCSI Devices', 'devices': devices,
                                 'device_count': len(devices),
                                 'show_all': show_all})

    def test_the_address_is_printed(self):
        html = self._render()
        self.assertIn('[16:0:0:0]', html)
        self.assertIn('[16:0:1:0]', html)
        self.assertNotIn('[:::]', html)

    def test_the_type_is_printed_and_coloured(self):
        html = self._render()
        self.assertIn('device-kind mediumx', html)
        self.assertIn('device-kind tape', html)
        self.assertIn('mediumx', html)
        self.assertIn('tape', html)

    def test_both_nodes_are_shown(self):
        """A changer has no /dev/st, and the generic node is the one mtx and
        our own commands are given."""
        html = self._render()
        self.assertIn('/dev/sg6', html)
        self.assertIn('/dev/st2', html)
        self.assertIn('/dev/sg7', html)

    def test_a_changer_keeps_its_revision(self):
        """lsscsi writes a bare "-" where a device has no primary node, and
        that column was read as the revision - pushing the real revision into
        the model, so the page showed "L700 0108" with a revision of "-"."""
        changer = self._devices()[0]
        self.assertEqual(changer.model, 'L700')
        self.assertEqual(changer.revision, '0108')
        html = self._render()
        self.assertIn('<td>L700</td>', html)
        self.assertIn('<td>0108</td>', html)

    def test_an_empty_list_says_so(self):
        html = self._render(devices=[])
        self.assertIn('No MHVTL devices found', html)


class DiskUsagePageTests(TestCase):
    """/libraries/console/disk/ shows what the disk service reports.

    It read `percent_used`, `total_mb`, `used_mb` and `free_mb`, none of which
    DiskUsage has, so on every host it drew an empty bar, a bare "%" and
    " MB" three times - and decided its own thresholds (70/90) besides. It now
    reads `percent`, `level` and the sizes df printed, as the dashboard does.
    """

    def _page(self, *disks):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from apps.libraries import console_views

        request = RequestFactory().get('/libraries/console/disk/')
        request.session = {'mhvtl_logged_in': True}
        request.user = AnonymousUser()
        data = {'data_dir': '/opt/mhvtl', 'config_dir': '/etc/mhvtl',
                'data_size': '1.7M', 'tape_count': 97, 'details': []}
        with mock.patch.object(console_views.disk, 'usage', return_value=list(disks)), \
                mock.patch.object(console_views, '_mhvtl_data', return_value=data):
            return console_views.DiskUsageView().get(request).content.decode()

    def _disk(self, percent, path='/opt/mhvtl'):
        from apps.libraries.services.console import disk
        return disk.DiskUsage(path=path, filesystem='/dev/sda2', size='62G',
                              used='45G', available='18G', percent=percent)

    def test_the_bar_and_the_figure_are_the_services_percent(self):
        body = self._page(self._disk(72))
        self.assertIn('style="width: 72%"', body)
        self.assertIn('>72%<', body)
        self.assertNotIn('width: %', body)

    def test_the_sizes_are_the_ones_df_printed(self):
        body = self._page(self._disk(72))
        for size in ('62G', '45G', '18G'):
            self.assertIn(size, body)
        self.assertNotIn(' MB<', body)

    def test_the_level_comes_from_the_service(self):
        """DiskUsage.level: warning from 75, danger from 90. 72 is normal here,
        though the template's own old rule (above 70) called it a warning."""
        for percent, level in ((72, 'normal'), (80, 'warning'), (93, 'danger')):
            with self.subTest(percent=percent):
                body = self._page(self._disk(percent))
                self.assertIn(f'progress-fill {level}"', body)
                self.assertIn(f'disk-percent {level}"', body)

    def test_a_full_disk_says_so(self):
        self.assertIn('critically low', self._page(self._disk(93)))
        self.assertIn('usage is high', self._page(self._disk(80)))
        self.assertNotIn('usage is high', self._page(self._disk(72)))
