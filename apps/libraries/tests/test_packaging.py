"""The version, in the places that must agree about it.

mhvtl_system/__init__.py is the one source: packaging/build.sh reads it and
stamps it into the spec it builds from, docs/sphinx/conf.py reads it, and the
page footer shows it through a context processor. The footer once said
"Version 1.0.0" under packages that said 2.0.0, because each place was written
out by hand.

Two things still cannot read it - the spec's own %define, which has to be
valid for anyone running rpmbuild by hand, and its %changelog - so they are
checked here instead.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

from mhvtl_system import __version__

GUI = Path(__file__).resolve().parents[3]
SPEC = GUI / 'packaging' / 'rpm' / 'mhvtl-gui.spec'
BUILD = GUI / 'packaging' / 'build.sh'


def documentation_trees():
    """The trees this checkout has, from `TREES =` in docs/Makefile.

    One list, read by `make strict` and by these tests. The development
    checkout lists all five; a published one lists what it publishes. A tree
    named there must exist and be complete - so the list cannot quietly lose
    one, and a checkout cannot claim one it does not have.
    """
    found = re.search(r'^TREES\s*=\s*(.+)$', (GUI / 'docs' / 'Makefile').read_text(), re.M)
    return tuple(found.group(1).split()) if found else ()


class VersionTests(SimpleTestCase):

    def test_the_spec_defines_the_same_version(self):
        """build.sh stamps this at build time, so a mismatch cannot ship - but
        a hand-run rpmbuild would build the old number with the new
        changelog."""
        found = re.search(r'^%define version (\S+)', SPEC.read_text(), re.M)
        self.assertIsNotNone(found, 'the spec has no %define version')
        self.assertEqual(found.group(1), __version__)

    def test_the_changelog_names_this_version(self):
        """An RPM whose newest changelog entry is for another version is one
        nobody can tell the age of."""
        found = re.search(r'^\* .* - (\S+?)-\d+$',
                          SPEC.read_text().split('%changelog', 1)[1], re.M)
        self.assertIsNotNone(found, 'no entries in %changelog')
        self.assertEqual(found.group(1), __version__)

    def test_build_sh_reads_the_version_rather_than_holding_one(self):
        text = BUILD.read_text()
        self.assertIn('__version__', text)
        self.assertIsNone(re.search(r'^VERSION="\d', text, re.M),
                          'build.sh has gone back to a literal version')

    def test_the_docs_read_the_version(self):
        for tree in documentation_trees():
            with self.subTest(tree=tree):
                text = (GUI / 'docs' / tree / 'conf.py').read_text()
                self.assertIn('__version__', text)
                self.assertIsNone(re.search(r"^release\s*=\s*'\d", text, re.M),
                                  'conf.py has gone back to a literal version')


class ScriptletTests(SimpleTestCase):
    """What the package does to the service when it is installed or upgraded."""

    def test_an_upgrade_restarts_the_service(self):
        """gunicorn workers hold the code they were started with, so without
        this an upgraded console keeps serving the previous version - the
        footer said 2.0.0 for twenty minutes after 2.1.0 was installed."""
        post = SPEC.read_text().split('%post', 1)[1].split('%preun', 1)[0]
        self.assertIn('try-restart', post)
        self.assertRegex(post, r'\[ \$1 -gt 1 \]',
                         'the restart must be for upgrades only')

    def test_a_first_install_is_not_told_to_restart_nothing(self):
        """try-restart, not restart: a fresh install has nothing running and
        should not report a failure for it."""
        post = SPEC.read_text().split('%post', 1)[1].split('%preun', 1)[0]
        self.assertNotRegex(post, r'systemctl restart ')


class DocumentationTreeTests(SimpleTestCase):
    """The documentation trees agree about the version, and are built.

    They are deliberately independent - own conf.py, own search, own Arabic
    catalogue - which means nothing keeps them in step except this. Which
    trees there are is docs/Makefile's TREES line: all five here, fewer in a
    published checkout.
    """

    TREES = documentation_trees()

    def test_there_are_trees(self):
        self.assertIn('user', self.TREES, 'docs/Makefile names no user guide')

    def _conf(self, tree):
        return (GUI / 'docs' / tree / 'conf.py').read_text()

    def test_every_tree_exists_and_can_be_built(self):
        for tree in self.TREES:
            with self.subTest(tree=tree):
                folder = GUI / 'docs' / tree
                self.assertTrue((folder / 'conf.py').is_file())
                self.assertTrue((folder / 'index.rst').is_file())
                self.assertTrue((folder / 'Makefile').is_file())

    def test_every_tree_reads_the_one_version(self):
        """Four hand-written version strings is four chances to publish a
        documentation set that disagrees with itself."""
        for tree in self.TREES:
            with self.subTest(tree=tree):
                self.assertIn('__version__', self._conf(tree))

    def test_every_tree_can_be_translated(self):
        """Arabic is not an afterthought here: a tree without a catalogue
        directory silently has no translations and never says so."""
        for tree in self.TREES:
            with self.subTest(tree=tree):
                conf = self._conf(tree)
                self.assertRegex(conf, r"locale_dirs\s*=\s*\['locale/'\]")
                # one .po per page, so a translator can take a single page
                self.assertRegex(conf, r'gettext_compact\s*=\s*False')
                self.assertTrue((GUI / 'docs' / tree / 'locale' / 'ar').is_dir(),
                                f'{tree} has no Arabic catalogue')


class CleanCheckoutTests(SimpleTestCase):
    """The suite must pass on a checkout nobody has run anything in.

    It did not: the testing settings pointed MHVTL_CONFIG_DIR at
    ./generated_configs/, a directory the development server creates. On a
    machine where somebody had run one, the suite was green; on a clean clone
    two tests read a device.conf that was not there. A backstop that exists
    only on some machines is not a backstop.
    """

    def test_the_test_config_is_not_a_path_in_the_checkout(self):
        from django.conf import settings

        config = Path(settings.MHVTL_CONFIG_DIR).resolve()
        self.assertFalse(str(config).startswith(str(GUI)),
                         f'{config} is inside the checkout; the suite would '
                         f'depend on whatever is left in it')

    def test_the_test_config_has_the_fixtures_in_it(self):
        """Seeded, not merely empty: tests that fall back to it need a
        device.conf to read."""
        from django.conf import settings

        config = Path(settings.MHVTL_CONFIG_DIR)
        self.assertTrue((config / 'device.conf').is_file())
        self.assertTrue((config / 'library_contents.10').is_file())

    def test_nothing_the_suite_needs_is_ignored_by_git(self):
        """A file the tests read must be in the repository, or the next clone
        cannot run them."""
        import subprocess

        fixtures = GUI / 'apps' / 'libraries' / 'tests' / 'fixtures'
        listed = subprocess.run(
            ['git', 'ls-files', str(fixtures.relative_to(GUI))],
            cwd=GUI, capture_output=True, text=True)
        tracked = {line.split('/')[-1] for line in listed.stdout.splitlines()}
        for needed in ('device.conf', 'library_contents.10'):
            self.assertIn(needed, tracked, f'{needed} is not tracked')
