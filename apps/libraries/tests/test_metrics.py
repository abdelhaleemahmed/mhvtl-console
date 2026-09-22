"""Per-library metrics: the daemon, its cost, its disk and its drives.

Step 8 of the service-layer refactor. systemctl, ps and df are faked: these
tests are about how their output is read, and metrics that depend on the host's
own load fail for reasons that have nothing to do with the code.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.console import disk, metrics, units
from apps.libraries.services.core.shell import CommandResult

FIXTURES = Path(__file__).parent / 'fixtures'


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


def failed(stderr='no'):
    return CommandResult(['fake'], 1, '', stderr)


class UptimeTests(TestCase):
    def _shown(self, value):
        return mock.patch.object(metrics.shell, 'run', return_value=ok(value))

    def test_reads_a_systemd_timestamp(self):
        from datetime import datetime, timedelta
        started = datetime.now() - timedelta(days=3, hours=4)
        stamp = started.strftime('%a %Y-%m-%d %H:%M:%S UTC')

        with self._shown(stamp):
            self.assertEqual(metrics.uptime('vtllibrary@10.service'), '3d 4h')

    def test_less_than_an_hour_is_minutes(self):
        from datetime import datetime, timedelta
        started = datetime.now() - timedelta(minutes=12)
        with self._shown(started.strftime('%a %Y-%m-%d %H:%M:%S UTC')):
            self.assertEqual(metrics.uptime('vtllibrary@10.service'), '12m')

    def test_a_unit_that_never_started_has_no_uptime(self):
        """The version this replaces returned the string "Running" here, so a
        stopped daemon's page said Running."""
        with self._shown('n/a'):
            self.assertIsNone(metrics.uptime('vtllibrary@10.service'))

    def test_an_unparseable_timestamp_is_none_not_a_guess(self):
        with self._shown('whenever'):
            self.assertIsNone(metrics.uptime('vtllibrary@10.service'))

    def test_a_failed_call_is_none(self):
        with mock.patch.object(metrics.shell, 'run', return_value=failed()):
            self.assertIsNone(metrics.uptime('vtllibrary@10.service'))


class ProcessTests(TestCase):
    def test_reads_cpu_memory_and_resident_size(self):
        with mock.patch.object(metrics.shell, 'run',
                               return_value=ok(' 2.5  0.3 12800\n')):
            found = metrics.process(4242)

        self.assertEqual((found.pid, found.cpu_percent, found.memory_percent),
                         (4242, 2.5, 0.3))
        self.assertEqual(found.memory_mb, 12.5)

    def test_a_pid_that_is_gone_is_none(self):
        with mock.patch.object(metrics.shell, 'run', return_value=failed()):
            self.assertIsNone(metrics.process(4242))

    def test_unreadable_output_is_none_not_a_crash(self):
        with mock.patch.object(metrics.shell, 'run', return_value=ok('? ? ?')):
            self.assertIsNone(metrics.process(4242))


class DiskSizeTests(TestCase):
    """df -h gives strings; a used bar needs numbers."""

    def test_reads_each_unit(self):
        self.assertEqual(disk._to_gb('920G'), 920.0)
        self.assertEqual(disk._to_gb('1.8T'), 1843.2)
        self.assertEqual(disk._to_gb('512M'), 0.5)

    def test_an_unparseable_size_is_none_not_zero(self):
        """A zero reads as an empty disk."""
        self.assertIsNone(disk._to_gb('-'))
        self.assertIsNone(disk._to_gb(''))

    def test_the_properties_reach_the_dict(self):
        usage = disk.DiskUsage(path='/opt/mhvtl', size='1.8T', used='920G',
                               available='800G', percent=53)
        data = usage.to_dict()
        self.assertEqual(data['used_gb'], 920.0)
        self.assertEqual(data['size_gb'], 1843.2)


class LibraryMetricsTests(TestCase):
    """The drive ids come from device.conf, not from a range."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        import shutil
        shutil.copy(FIXTURES / 'device.conf', self.config)

        patches = {
            'uptime': mock.patch.object(metrics, 'uptime', return_value='2h 5m'),
            'process': mock.patch.object(metrics, 'process', return_value=None),
            'pids': mock.patch.object(units, 'pids_of', return_value=[4242]),
            'usage': mock.patch.object(disk, 'usage', return_value=[]),
        }
        for name, patch in patches.items():
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def _active(self, active_units):
        def is_active(unit):
            return unit in active_units

        def _is(unit, question):
            return 'active' if unit in active_units else 'inactive'

        return (mock.patch.object(units, 'is_active', side_effect=is_active),
                mock.patch.object(units, '_is', side_effect=_is))

    def test_the_drive_total_comes_from_device_conf(self):
        active, is_ = self._active({'vtllibrary@10.service'})
        with active, is_:
            found = metrics.for_library(10, config_dir=self.config)

        self.assertEqual(found.drives_total, 4)     # 11, 12, 13, 14
        self.assertEqual(found.drives_online, 0)

    def test_counts_only_the_drives_that_are_running(self):
        active, is_ = self._active({'vtllibrary@10.service', 'vtltape@11.service',
                                    'vtltape@13.service'})
        with active, is_:
            found = metrics.for_library(10, config_dir=self.config)

        self.assertEqual((found.drives_online, found.drives_total), (2, 4))
        self.assertFalse(found.healthy)

    def test_all_drives_running_is_healthy(self):
        active, is_ = self._active({f'vtltape@{n}.service' for n in (11, 12, 13, 14)}
                                   | {'vtllibrary@10.service'})
        with active, is_:
            self.assertTrue(metrics.for_library(10, config_dir=self.config).healthy)

    def test_a_stopped_library_is_not_healthy_however_many_drives_run(self):
        active, is_ = self._active({f'vtltape@{n}.service' for n in (11, 12, 13, 14)})
        with active, is_:
            found = metrics.for_library(10, config_dir=self.config)
        self.assertFalse(found.running)
        self.assertFalse(found.healthy)

    def test_a_stopped_library_has_no_process_or_uptime(self):
        active, is_ = self._active(set())
        with active, is_:
            found = metrics.for_library(10, config_dir=self.config)

        self.assertIsNone(found.uptime)
        self.assertIsNone(found.process)

    def test_an_unreadable_device_conf_reports_no_drives_not_guessed_ones(self):
        active, is_ = self._active({'vtllibrary@10.service'})
        with active, is_:
            found = metrics.for_library(10, config_dir=tempfile.mkdtemp())

        self.assertEqual((found.drives_online, found.drives_total), (0, 0))

    def test_drive_ids_can_be_supplied_by_a_caller_that_has_them(self):
        active, is_ = self._active({'vtltape@41.service'})
        with active, is_:
            found = metrics.for_library(40, drive_ids=[41, 42],
                                        config_dir=self.config)
        self.assertEqual((found.drives_online, found.drives_total), (1, 2))


class ApiResponseTests(TestCase):
    """The monitoring page reads this field by field (views._monitor_metrics)."""

    def response(self):
        from apps.libraries.views import _monitor_metrics
        return _monitor_metrics(10)

    def _collected(self, **overrides):
        base = dict(library_id=10, unit='vtllibrary@10.service', running=True,
                    state='active', uptime='2h 5m',
                    process=metrics.ProcessMetrics(4242, 2.5, 0.3, 12.5),
                    disk_usage=disk.DiskUsage(path='/opt/mhvtl', size='1.8T',
                                              used='920G', available='800G',
                                              percent=53),
                    drives_online=4, drives_total=4)
        base.update(overrides)
        return metrics.LibraryMetrics(**base)

    def _with(self, collected):
        return (mock.patch.object(metrics, 'for_library', return_value=collected),
                mock.patch.object(units, 'pids_of', return_value=[4242]),
                mock.patch.object(units, 'is_enabled', return_value=True))

    def test_every_field_the_template_reads_is_present(self):
        for patch in self._with(self._collected()):
            patch.start()
            self.addCleanup(patch.stop)

        response = self.response()

        self.assertTrue(response['is_running'])
        self.assertEqual(response['service_status']['pid'], 4242)
        self.assertEqual(response['metrics']['cpu'], 2.5)
        self.assertEqual(response['metrics']['disk'], 53)
        self.assertEqual(response['metrics']['disk_used_gb'], 920.0)
        self.assertEqual(response['drive_status'], {'online': 4, 'total': 4})

    def test_a_stopped_library_reads_stopped_not_a_blank(self):
        for patch in self._with(self._collected(running=False, uptime=None,
                                                process=None, drives_online=0)):
            patch.start()
            self.addCleanup(patch.stop)

        response = self.response()
        self.assertEqual(response['metrics']['uptime'], 'Stopped')
        self.assertIsNone(response['metrics']['cpu'])

    def test_the_drive_count_comes_from_device_conf(self):
        """The database's count used to be passed in; device.conf is the
        authority, and for_library reads it."""
        for patch in self._with(self._collected(drives_total=4, drives_online=3)):
            patch.start()
            self.addCleanup(patch.stop)

        self.assertEqual(self.response()['drive_status'], {'online': 3, 'total': 4})
