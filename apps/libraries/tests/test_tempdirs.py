"""No test leaves a temporary directory behind.

The suite called tempfile.mkdtemp 89 times and removed nothing. Every run
abandoned its directories, and a test that had written a cartridge abandoned
120 MB with it, because a tape is a file. On the development host that
reached 17.5 GB and 168,510 entries in /tmp - past the point where a glob
works at all: `rm -rf /tmp/tmp*` dies with "argument list too long", and
`du /tmp` has to walk all of it before it can tell you so.

The fix is tmpdir() on the base classes in base.py, which registers its own
removal. These tests are what stop the ninety-first call site going back to
mkdtemp - a cleanup rule nothing enforces is a cleanup rule that decays.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TESTS = Path(__file__).resolve().parent

#: ``tempfile.mkdtemp(...)`` or a bare ``mkdtemp(...)``.
MKDTEMP = re.compile(r'\bmkdtemp\s*\(')

#: base.py is where the one permitted call lives.
OWNS_THE_RULE = 'base.py'

#: This module quotes the pattern it is looking for, in the test that proves
#: the check can fail. Scanning itself would always find it.
THIS_MODULE = Path(__file__).name


def test_modules():
    for path in sorted(TESTS.glob('test_*.py')):
        if path.name == THIS_MODULE:
            continue
        yield path.name, path.read_text()


class NoDirectMkdtempTests(SimpleTestCase):

    def test_no_test_module_calls_mkdtemp_directly(self):
        wrong = [f'{name}: calls mkdtemp directly - use self.tmpdir(), '
                 f'which cleans up after the test'
                 for name, text in test_modules() if MKDTEMP.search(text)]
        self.assertEqual(wrong, [], '\n'.join(wrong))

    def test_base_is_the_only_place_that_calls_it(self):
        base = (TESTS / OWNS_THE_RULE).read_text()
        self.assertEqual(len(MKDTEMP.findall(base)), 1,
                         'base.py should call mkdtemp exactly once, in tmpdir()')

    def test_the_check_would_notice_one(self):
        """A test that cannot fail protects nothing."""
        self.assertTrue(MKDTEMP.search('x = tempfile.mkdtemp()'))
        self.assertTrue(MKDTEMP.search('d = mkdtemp(dir=base)'))
        self.assertIsNone(MKDTEMP.search('self.tmpdir()'))


class TmpdirTests(SimpleTestCase):
    """The helper itself."""

    def test_it_gives_a_fresh_empty_directory(self):
        from .base import TempDirMixin

        class Probe(TempDirMixin, SimpleTestCase):
            def runTest(self):
                pass

        probe = Probe()
        first, second = probe.tmpdir(), probe.tmpdir()
        self.addCleanup(probe.doCleanups)
        self.assertTrue(first.is_dir())
        self.assertNotEqual(first, second)
        self.assertEqual(list(first.iterdir()), [])

    def test_it_is_gone_once_the_test_finishes(self):
        from .base import TempDirMixin

        class Probe(TempDirMixin, SimpleTestCase):
            def runTest(self):
                pass

        probe = Probe()
        path = probe.tmpdir()
        (path / 'cartridge').write_bytes(b'x' * 1024)
        self.assertTrue(path.exists())
        probe.doCleanups()
        self.assertFalse(path.exists(), 'tmpdir() outlived its test')

    def test_a_directory_the_test_already_removed_is_not_an_error(self):
        """test_lifecycle deletes the directory it was given; a cleanup that
        raised would turn a passing test into an error."""
        import shutil

        from .base import TempDirMixin

        class Probe(TempDirMixin, SimpleTestCase):
            def runTest(self):
                pass

        probe = Probe()
        path = probe.tmpdir()
        shutil.rmtree(path)
        probe.doCleanups()                      # must not raise

    def test_a_nested_one_is_removed_too(self):
        from .base import TempDirMixin

        class Probe(TempDirMixin, SimpleTestCase):
            def runTest(self):
                pass

        probe = Probe()
        base = probe.tmpdir()
        nested = probe.tmpdir(parent=base)
        self.assertTrue(str(nested).startswith(str(base)))
        probe.doCleanups()
        self.assertFalse(base.exists())
