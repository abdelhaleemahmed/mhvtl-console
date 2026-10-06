"""The service layer's dependency graph, checked rather than remembered.

services/__init__.py states the layering as rule 7 and lists the graph. A comment
does not stop anyone breaking it, and it very nearly was broken: reading a
library's drives inside tapes/ by importing operations.mounting would have closed
a cycle, because operations/ imports tapes/ (mounting.py:25). The answer was to
ask profiles/ and config/ instead - both already tapes' dependencies - but only
because the direction was checked first.

So this computes the graph from the source and fails on a cycle. It is cheap, it
needs no configuration and no hardware, and it is the only thing standing between
the design and an ordinary afternoon's convenience.
"""
import re
from collections import defaultdict
from pathlib import Path

from django.test import TestCase

SERVICES = Path(__file__).resolve().parent.parent / 'services'

#: What each package may import. Empty means it must import nothing from a
#: sibling at all. Kept as data so a deliberate change is a visible diff.
ALLOWED = {
    'core': set(),
    'profiles': set(),
    'config': {'core', 'profiles'},
    # core since 5 October 2026: sync is a service like any other and returns
    # a ServiceResult, which lives there. It was the one package that did
    # not, and that is why `mhvtl config sync` printed a traceback for an
    # unreadable device.conf - the function raised instead of reporting.
    'sync': {'config', 'core'},
    'console': {'config', 'core'},
    'scsi': {'config', 'core'},
    'tapes': {'config', 'core', 'profiles'},
    'operations': {'config', 'core', 'profiles', 'scsi', 'tapes'},
    'ltfs': {'core', 'operations', 'profiles', 'scsi', 'tapes'},
    'iscsi': {'config', 'console', 'core', 'scsi'},
    'drives': {'config', 'console', 'core', 'iscsi', 'operations', 'profiles',
               'sync'},
    'verification': {'config', 'core', 'operations', 'scsi'},
    'libraries': {'config', 'console', 'core', 'drives', 'iscsi', 'operations',
                  'profiles', 'sync', 'tapes'},
    'about': {'console', 'core'},
    'dashboard': {'config', 'console', 'libraries'},
}

RELATIVE = re.compile(r'^\s*from \.\.(\w+)', re.M)
ABSOLUTE = re.compile(r'^\s*from apps\.libraries\.services\.(\w+)', re.M)

#: A real import of the ORM, not the rule text that every module's header quotes.
ORM_IMPORT = re.compile(
    r'^\s*(?:from apps\.libraries\.models import|import apps\.libraries\.models'
    r'|from apps\.libraries import .*\bmodels\b)', re.M)


def packages():
    return sorted(p.name for p in SERVICES.iterdir()
                  if p.is_dir() and (p / '__init__.py').exists())


def graph():
    names = set(packages())
    edges = defaultdict(set)
    for package in names:
        for source in (SERVICES / package).rglob('*.py'):
            text = source.read_text()
            for pattern in (RELATIVE, ABSOLUTE):
                for match in pattern.finditer(text):
                    imported = match.group(1)
                    if imported in names and imported != package:
                        edges[package].add(imported)
    return edges


class ServiceLayerTests(TestCase):

    def setUp(self):
        self.edges = graph()

    def test_every_package_is_accounted_for(self):
        """A new package must be given a row, so its place is a decision."""
        self.assertEqual(set(packages()), set(ALLOWED),
                         'a service package was added or removed without '
                         'deciding where it sits; update ALLOWED and rule 7')

    def test_nothing_imports_more_than_its_row_allows(self):
        for package, allowed in sorted(ALLOWED.items()):
            with self.subTest(package=package):
                self.assertLessEqual(
                    self.edges[package], allowed,
                    f'{package}/ imports '
                    f'{sorted(self.edges[package] - allowed)}, which rule 7 '
                    f'does not allow. If that is deliberate, change ALLOWED '
                    f'and the graph in services/__init__.py together.')

    def test_the_graph_has_no_cycles(self):
        """Two packages that import each other cannot be reasoned about, and
        break on import order rather than on a test."""
        pairs = sorted((a, b) for a in self.edges for b in self.edges[a]
                       if a in self.edges.get(b, ()))
        self.assertEqual(pairs, [], f'import cycle(s): {pairs}')

    def test_core_and_profiles_depend_on_nothing(self):
        """They are the bottom. Everything may read them; they read nobody."""
        self.assertEqual(self.edges['core'], set())
        self.assertEqual(self.edges['profiles'], set())

    def test_tapes_stays_low(self):
        """tapes/ may not import operations/, scsi/, drives/ or ltfs/.

        operations/ imports tapes/ (mounting.py), so tapes -> operations would
        close a cycle. When tapes/ needs to know whether LTFS would open a
        drive, it asks profiles/ for the table and config/ for the file.
        """
        for forbidden in ('operations', 'scsi', 'drives', 'ltfs', 'libraries'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.edges['tapes'])

    def test_no_service_package_imports_ltfs(self):
        """ltfs/ is a leaf: the views and the CLI call it, no service does.

        LTFS is a feature layered on tapes, not something tapes knows about.
        """
        for package in packages():
            if package == 'ltfs':
                continue
            with self.subTest(package=package):
                self.assertNotIn('ltfs', self.edges[package])

    def test_only_sync_imports_the_orm(self):
        """Rule 4, checked the same way as rule 7 rather than by review.

        Matched as an import statement, not as the string: every module's header
        quotes the rule itself, so searching for the name finds the whole layer.
        """
        for package in packages():
            for source in (SERVICES / package).rglob('*.py'):
                if not ORM_IMPORT.search(source.read_text()):
                    continue
                with self.subTest(source=str(source.relative_to(SERVICES))):
                    self.assertEqual(package, 'sync',
                                     'only sync/ may import the ORM')
