"""Every service and CLI module appears in the API reference.

The API reference is not generated from the tree: ``docs/api/services.rst``
and ``docs/api/cli.rst`` list their ``automodule`` directives by hand, with a
sentence of their own in front of each. That is deliberate - the ordering and
the prose are worth writing - but it means adding a module adds nothing to the
reference, and Sphinx cannot warn about a module it was never told exists.

Thirteen had accumulated that way, among them the whole ``services.ltfs``
package and ``services.tapes.palette``, before anyone compared the built site
against the modules on disk. Hence this.

Nothing is imported here: the directives are read as text, so a module that
cannot import on this host still counts as documented.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

GUI = Path(__file__).resolve().parents[3]

#: The packages whose every module must be in the reference, and the import
#: path each one is written under.
PACKAGES = {
    GUI / 'apps' / 'libraries' / 'services': 'apps.libraries.services',
    GUI / 'mhvtl_cli': 'mhvtl_cli',
}

#: ``__init__.py`` carries the package's own header rather than an API of its
#: own, and the reference introduces each package in prose instead.
NOT_A_PAGE = {'__init__.py'}


def documented():
    """Every module named by an automodule directive in docs/api."""
    directive = re.compile(r'automodule:: (\S+)')
    found = set()
    for page in sorted((GUI / 'docs' / 'api').glob('*.rst')):
        found |= set(directive.findall(page.read_text()))
    return found


def modules_on_disk():
    """Dotted names of every module in PACKAGES."""
    for root, package in PACKAGES.items():
        for path in sorted(root.rglob('*.py')):
            if path.name in NOT_A_PAGE or '__pycache__' in path.parts:
                continue
            parts = path.relative_to(root).with_suffix('').parts
            yield '.'.join((package,) + parts), path.relative_to(GUI)


class ApiReferenceCoverageTests(SimpleTestCase):

    def test_every_module_has_an_api_page(self):
        pages = documented()
        self.assertTrue(pages, 'no automodule directives found in docs/api')

        missing = [f'{path}: no automodule:: {module} in docs/api'
                   for module, path in modules_on_disk()
                   if module not in pages]
        self.assertEqual(missing, [], '\n'.join(missing))

    def test_every_documented_module_exists(self):
        """The other direction: a renamed module leaves a dead directive."""
        gone = []
        for module in sorted(documented()):
            parts = module.split('.')
            if parts[0] not in ('apps', 'mhvtl_cli', 'mhvtl_system'):
                continue
            path = GUI.joinpath(*parts)
            if not path.with_suffix('.py').exists() and not path.is_dir():
                gone.append(f'docs/api: automodule:: {module} - no such module')
        self.assertEqual(gone, [], '\n'.join(gone))

    def test_the_check_would_notice_a_missing_one(self):
        """A test that cannot fail protects nothing."""
        self.assertNotIn('apps.libraries.services.ltfs.invented', documented())
        self.assertIn('apps.libraries.services.ltfs.service', documented())
