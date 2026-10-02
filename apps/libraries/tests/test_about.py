"""The console knows what version it is, and says the same thing twice.

The footer once said 1.0.0 while the packages said 2.0.0. The fix was to put
the version in one place; these tests are what keep it there, because the
duplicate that packaging needs cannot be removed - pyproject.toml has to be
declarative, so it carries its own copy of the same four facts.

Both files are in this repository and both are always present, so holding
them to each other is a check that works for anyone who clones. That is the
difference between this and the sudoers check that could only pass on one
machine.
"""
import re
import tomllib
from pathlib import Path

from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase

import mhvtl_system

from apps.libraries.services import about

GUI = Path(__file__).resolve().parents[3]


def pyproject():
    with open(GUI / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)['project']


class ProjectIdentityTests(SimpleTestCase):

    def test_pyproject_agrees_about_the_version(self):
        self.assertEqual(pyproject()['version'], mhvtl_system.__version__)

    def test_pyproject_agrees_about_the_author(self):
        authors = pyproject()['authors']
        self.assertEqual(len(authors), 1, f'expected one author: {authors}')
        self.assertEqual(authors[0]['name'], mhvtl_system.__author__)
        self.assertEqual(authors[0]['email'], mhvtl_system.__email__)

    def test_pyproject_agrees_about_the_licence(self):
        licence = pyproject()['license']
        text = licence if isinstance(licence, str) else licence['text']
        self.assertEqual(text, mhvtl_system.__licence__)

    def test_the_packaged_spec_names_the_same_author(self):
        """The RPM's Packager: line is a third copy, and it reaches users."""
        spec = (GUI / 'packaging/rpm/mhvtl-gui.spec').read_text()
        expected = f'{mhvtl_system.__author__} <{mhvtl_system.__email__}>'
        self.assertIn(f'Packager:       {expected}', spec)
        self.assertIn(f'License:        {mhvtl_system.__licence__}', spec)

    def test_the_check_would_notice_a_disagreement(self):
        """A test that cannot fail protects nothing."""
        self.assertNotEqual(mhvtl_system.__version__, '1.1.1')
        self.assertNotEqual(mhvtl_system.__licence__, 'GPLv3')


class AboutServiceTests(SimpleTestCase):

    def test_project_needs_no_io_and_cannot_fail(self):
        it = about.project()
        self.assertEqual(it['version'], mhvtl_system.__version__)
        self.assertEqual(it['author'], 'Ahmed Abdelhaleem Ahmed')
        self.assertEqual(it['email'], 'ahmedhal@gmail.com')
        self.assertEqual(it['name'], 'mhvtl-gui')

    def test_facts_returns_a_service_result(self):
        result = about.facts()
        self.assertTrue(result.success)
        self.assertIn('project', result.data)
        self.assertIn('runtime', result.data)
        self.assertEqual(result.data['project']['version'],
                         mhvtl_system.__version__)

    def test_the_runtime_half_reports_what_a_bug_report_needs(self):
        runtime = about.facts().data['runtime']
        for key in ('python', 'django', 'platform', 'hostname'):
            self.assertIn(key, runtime)

    def test_one_line_carries_the_version_and_where_to_report_it(self):
        text = about.one_line()
        self.assertIn(mhvtl_system.__version__, text)
        self.assertIn('Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com>', text)
        self.assertIn(mhvtl_system.__licence__, text)
        self.assertIn('github.com/abdelhaleemahmed/mhvtl-console', text)

    def test_a_failing_collector_costs_its_own_line_only(self):
        """A wedged systemctl must not cost the version above it."""
        with patch('apps.libraries.services.console.units.status',
                   side_effect=OSError('no queue')):
            result = about.facts()
        self.assertTrue(result.success)
        self.assertEqual(result.data['project']['version'],
                         mhvtl_system.__version__)
        self.assertIn('no queue', result.data['runtime']['mhvtl_error'])


class AboutPageTests(SimpleTestCase):
    """The page itself. It formats what the service decided and nothing more.

    The view is called directly with a RequestFactory, the way the other page
    tests here do it: going through the test client would meet the security
    middleware's HTTPS redirect first and answer 302 for a reason that has
    nothing to do with this page.
    """

    def _body(self, session=None):
        from apps.libraries import views
        request = RequestFactory().get('/libraries/about/')
        request.session = {'mhvtl_logged_in': True} if session is None else session
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        return views.AboutView().get(request)

    def test_it_shows_the_version_the_author_and_the_licence(self):
        body = self._body().content.decode()
        self.assertIn(mhvtl_system.__version__, body)
        self.assertIn(mhvtl_system.__author__, body)
        self.assertIn(mhvtl_system.__email__, body)
        self.assertIn(mhvtl_system.__licence__, body)

    def test_the_email_is_a_mailto_link(self):
        body = self._body().content.decode()
        self.assertIn(f'mailto:{mhvtl_system.__email__}', body)

    def test_it_links_the_published_documentation(self):
        body = self._body().content.decode()
        self.assertIn(mhvtl_system.DOCS_USER, body)
        self.assertIn(mhvtl_system.DOCS_API, body)

    def test_it_names_the_commands_that_answer_the_same_question(self):
        body = self._body().content.decode()
        self.assertIn('mhvtl --version', body)
        self.assertIn('mhvtl status system', body)

    def test_it_needs_a_login(self):
        response = self._body(session={})
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_a_failed_collector_says_so_rather_than_leaving_a_blank(self):
        """A blank row reads as "nothing here", which is a different answer."""
        with patch('apps.libraries.services.console.units.status',
                   side_effect=OSError('no queue')):
            body = self._body().content.decode()
        self.assertIn('could not be read', body)
        self.assertIn('no queue', body)
        # and the version above it survives
        self.assertIn(mhvtl_system.__version__, body)


#: Every complete ``<html>`` document in this project, and whether it is
#: reached before a login. There are six, not the four the plan said: the
#: authentication dashboard and the login page are whole documents of their
#: own too, neither extending anything. That is why the version reached 8
#: pages of 46 - the footer lives in one shell out of six.
SHELLS = {
    'templates/base.html': True,
    'apps/libraries/templates/libraries/base.html': True,
    'apps/libraries/templates/libraries/operator/base.html': True,
    'apps/libraries/templates/libraries/console/base.html': True,
    'apps/authentication/templates/authentication/dashboard.html': True,
    #: The only page reached without a login. The version is not a secret, but
    #: there is no reason to tell an unauthenticated visitor which release to
    #: look up - the same decision the About page makes by requiring a login.
    'apps/authentication/templates/authentication/login.html': False,
}

#: ``{% include 'x/y.html' %}``, so a shell is read together with what it
#: pulls in.
INCLUDE = re.compile(r"""\{%\s*include\s+['"]([^'"]+)['"]""")

#: Where Django looks for templates. Scanning the whole tree also finds the
#: HTML that Playwright vendors into a recording kit's .venv, which is not ours.
TEMPLATE_ROOTS = ('templates', 'apps/authentication/templates',
                  'apps/libraries/templates')


class EveryShellTests(SimpleTestCase):
    """The test that would have caught the gap this work exists to fix.

    Collected by walking the template roots rather than listing the shells by
    hand, so a seventh shell added later is held to the same rule.
    """

    def _expand(self, root, text, seen=None):
        """A shell's text, with what it includes folded in.

        templates/base.html carries the About link in includes/_header.html
        and the version in includes/_footer.html, so reading the shell alone
        would call it missing both.
        """
        seen = seen if seen is not None else set()
        for name in INCLUDE.findall(text):
            if name in seen:
                continue
            seen.add(name)
            for candidate in TEMPLATE_ROOTS:
                path = GUI / candidate / name
                if path.exists():
                    included = path.read_text(errors='replace')
                    text += '\n' + self._expand(root, included, seen)
                    break
        return text

    def shells(self):
        """Every template that is a whole document rather than a fragment."""
        found = []
        for root in TEMPLATE_ROOTS:
            for path in sorted((GUI / root).rglob('*.html')):
                text = path.read_text(errors='replace')
                if '<!DOCTYPE' in text and '{% extends' not in text:
                    found.append((str(path.relative_to(GUI)),
                                  self._expand(root, text)))
        return found

    def test_the_shells_are_still_the_shells_we_know_about(self):
        """If this fails a shell was added or removed - decide, then update."""
        self.assertEqual(sorted(name for name, _ in self.shells()),
                         sorted(SHELLS))

    def test_every_shell_behind_a_login_links_to_about(self):
        missing = [name for name, text in self.shells()
                   if SHELLS[name] and 'libraries:about' not in text]
        self.assertEqual(missing, [], f'no About link in: {missing}')

    def test_every_shell_behind_a_login_shows_the_version(self):
        """Directly, or by including something that does."""
        missing = []
        for name, text in self.shells():
            if not SHELLS[name]:
                continue
            if 'gui_version' not in text:
                missing.append(name)
        self.assertEqual(missing, [], f'no version shown in: {missing}')

    def test_the_login_page_says_nothing_about_the_version(self):
        """Deliberate, not an oversight: it is the one page reached without
        a login, and it should not hand a visitor a release number."""
        login = dict(self.shells())[
            'apps/authentication/templates/authentication/login.html']
        self.assertNotIn('gui_version', login)


class TemplateCommentTests(SimpleTestCase):
    """No template leaks a comment into the page.

    Django's ``{# #}`` is **single-line only**. Spread one over two lines and
    the engine does not recognise it, so the text renders - visible to whoever
    opened the page. It has happened three times on the mount page, it is
    written up in architecture.rst, and it happened again on the About page
    while that very warning was being written. A sweep is cheaper than
    noticing.

    ``{% comment %} ... {% endcomment %}`` is the multi-line form.
    """

    #: ``{# ... #}``, across lines if need be, so a two-line one is found.
    COMMENT = re.compile(r'\{#(.*?)#\}', re.S)

    def templates(self):
        for root in TEMPLATE_ROOTS:
            for path in sorted((GUI / root).rglob('*.html')):
                yield str(path.relative_to(GUI)), path.read_text(errors='replace')

    def test_no_template_has_a_multi_line_hash_comment(self):
        wrong = []
        for name, text in self.templates():
            for match in self.COMMENT.finditer(text):
                if '\n' in match.group(1):
                    first = match.group(1).strip().splitlines()[0][:50]
                    wrong.append(f'{name}: {{# {first}... #}} spans lines - '
                                 f'use {{% comment %}}')
        self.assertEqual(wrong, [], '\n'.join(wrong))

    def test_the_rendered_about_page_leaks_no_comment(self):
        """Rendered, not the template: that is the difference that matters."""
        from apps.libraries import views
        request = RequestFactory().get('/libraries/about/')
        request.session = {'mhvtl_logged_in': True}
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        body = views.AboutView().get(request).content.decode()
        self.assertNotIn('{#', body)
        self.assertNotIn('#}', body)
        self.assertNotIn('{% comment', body)

    def test_the_check_would_notice_one(self):
        """A test that cannot fail protects nothing."""
        self.assertTrue(
            any('\n' in m.group(1)
                for m in self.COMMENT.finditer('{# one\ntwo #}')))
