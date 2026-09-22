"""Mistakes a test run would only find on the path that hits them.

views.py used `logger` in four except branches without ever defining it, so
each of those error paths raised NameError instead of reporting the error.
pyflakes finds that class of mistake without running anything; this runs it
over the application code and fails on undefined names.

Skipped when pyflakes is not installed (it is in requirements-dev.txt).
"""
import io
import unittest
from pathlib import Path

from django.test import TestCase

try:
    from pyflakes import api as pyflakes_api
    from pyflakes import messages as pyflakes_messages
    from pyflakes import reporter as pyflakes_reporter
except ImportError:                                    # pragma: no cover
    pyflakes_api = None

GUI_ROOT = Path(__file__).resolve().parents[3]
CHECKED = ('apps', 'mhvtl_cli', 'mhvtl_system')

#: The kinds of finding that are bugs rather than style.
FATAL = ('UndefinedName', 'UndefinedLocal', 'UndefinedExport')


@unittest.skipIf(pyflakes_api is None, 'pyflakes is not installed')
class UndefinedNameTests(TestCase):

    def test_no_undefined_names(self):
        fatal = tuple(getattr(pyflakes_messages, name) for name in FATAL)
        found = []

        class Collect(pyflakes_reporter.Reporter):
            def flake(self, message):
                if isinstance(message, fatal):
                    found.append(str(message))

        collect = Collect(io.StringIO(), io.StringIO())
        for top in CHECKED:
            for path in sorted((GUI_ROOT / top).rglob('*.py')):
                if 'migrations' in path.parts:
                    continue
                pyflakes_api.checkPath(str(path), collect)
        self.assertEqual(found, [])
