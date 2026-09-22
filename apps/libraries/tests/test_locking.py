"""The lock around the configuration files.

The web process runs as an unprivileged user and /etc/mhvtl is root-owned and
group-readable, so it cannot create the lock file - and creating a library from
the GUI failed with "Permission denied: /etc/mhvtl/.mhvtl-config.lock", after
which nothing that writes configuration worked. flock does not need write
access: the lock is created with sudo when it is missing and opened read-only.
"""
import os
import stat
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.libraries.services.core import shell
from apps.libraries.services.core.locking import FileLock, LockTimeout


class LockTests(TestCase):

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.lock = self.directory / '.mhvtl-config.lock'

    def read_only_directory(self):
        """The mode a normal install has: readable, not writable."""
        os.chmod(self.directory, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.directory, 0o700)

    def test_it_locks_and_releases(self):
        with FileLock(self.lock):
            self.assertTrue(self.lock.exists())
        with FileLock(self.lock):                       # free again
            pass

    def test_the_holder_writes_its_pid(self):
        with FileLock(self.lock):
            self.assertEqual(self.lock.read_text().strip(), str(os.getpid()))

    def test_it_is_reentrant_in_one_thread(self):
        """Creating a library takes the lock and calls a service that takes it."""
        with FileLock(self.lock):
            with FileLock(self.lock):
                pass
            self.assertTrue(self.lock.exists())

    def test_another_thread_waits_and_gives_up(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with FileLock(self.lock):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            held.wait(5)
            with self.assertRaises(LockTimeout):
                with FileLock(self.lock, timeout_s=0.3):
                    pass
        finally:
            release.set()
            thread.join(5)

    def test_a_lock_file_this_user_cannot_write_still_locks(self):
        """On the host the file is root-owned 0644; flock works on a read-only
        file descriptor."""
        self.lock.touch()
        os.chmod(self.lock, stat.S_IRUSR)                # as root's file looks
        self.addCleanup(os.chmod, self.lock, 0o600)
        self.read_only_directory()
        with mock.patch.object(shell, 'sudo') as sudo:
            with FileLock(self.lock) as lock:
                self.assertFalse(lock._writable)
        sudo.assert_not_called()

    def test_a_missing_lock_in_an_unwritable_directory_is_created_with_sudo(self):
        self.read_only_directory()
        created = []

        def fake_sudo(argv, **kwargs):
            os.chmod(self.directory, 0o700)              # stand in for root
            Path(argv[-1]).touch()
            os.chmod(self.directory, stat.S_IRUSR | stat.S_IXUSR)
            created.append(list(argv))
            return shell.CommandResult(list(argv), 0, '', '')

        with mock.patch.object(shell, 'sudo', side_effect=fake_sudo):
            with FileLock(self.lock):
                pass
        self.assertEqual(created, [['touch', str(self.lock)]])

    def test_a_lock_that_cannot_be_created_says_so(self):
        self.read_only_directory()
        with mock.patch.object(shell, 'sudo',
                               return_value=shell.CommandResult([], 1, '', 'nope')):
            with self.assertRaises(PermissionError) as refused:
                with FileLock(self.lock):
                    pass
        self.assertIn('cannot create the lock file', str(refused.exception))

    def test_a_read_only_lock_still_excludes_another_thread(self):
        self.lock.touch()
        os.chmod(self.lock, stat.S_IRUSR)
        self.addCleanup(os.chmod, self.lock, 0o600)
        self.read_only_directory()
        held = threading.Event()
        release = threading.Event()

        def holder():
            with FileLock(self.lock):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            held.wait(5)
            with self.assertRaises(LockTimeout):
                with FileLock(self.lock, timeout_s=0.3):
                    pass
        finally:
            release.set()
            thread.join(5)
