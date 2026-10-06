"""Every page in the console, in the one frame.

There were five frames and fifty pages. No two of the five offered the same
navigation: the word *Libraries* went to ``/libraries/`` from the pages on
``templates/base.html`` and to ``/libraries/list/`` from the rest, the tape
operations and system console sections offered no way back to the libraries
at all, and the operator's name appeared on eight pages out of fifty. The
create-a-library form was reachable from one page - ``/libraries/`` - that
two of the frames never linked to, so from most of the console there was no
way to reach it. See guides/plan-one-console.

This walks the URL map rather than listing pages by hand, which is the whole
point: a page added to a section tomorrow is held to the same rule without
anybody remembering to add it here. It is also what found, when it was first
written as a script, that three pages already had nothing linking to them.
"""
import re

from django.contrib.auth import get_user_model
from django.urls import get_resolver

from .base import TestCase


def every_page():
    """``[(name, path)]`` for every route that renders a template.

    Routes taking an argument get one that exists on the test host. A route
    this cannot fill is skipped rather than guessed at - and said so, in
    SKIPPED, so a new argument type cannot quietly drop a page out of the
    sweep.
    """
    found, skipped = [], []

    def walk(resolver, prefix='', namespace=''):
        for entry in resolver.url_patterns:
            if hasattr(entry, 'url_patterns'):
                walk(entry, prefix + str(entry.pattern),
                     entry.namespace or namespace)
                continue
            if not entry.name:
                continue
            view = entry.callback
            template = (getattr(view, 'template_name', None)
                        or getattr(getattr(view, 'view_class', None),
                                   'template_name', None))
            if not template:
                continue
            path = '/' + prefix + str(entry.pattern)
            name = f'{namespace}:{entry.name}' if namespace else entry.name
            if '<' in path:
                path = re.sub(r'<int:drive_id>', '11', path)
                path = re.sub(r'<int:[^>]+>', '10', path)
                path = re.sub(r'<str:brand_name>', 'IBM', path)
                path = re.sub(r'<str:[^>]+>', 'x', path)
                if '<' in path:
                    skipped.append(name)
                    continue
            found.append((name, path))

    walk(get_resolver())
    return sorted(set(found)), sorted(set(skipped))


class OneFrameTests(TestCase):
    """What every page carries, because one frame carries it."""

    #: Routes whose argument this sweep cannot fill. Empty, and meant to
    #: stay that way: a page it cannot reach is a page it cannot check.
    SKIPPED = []

    def setUp(self):
        user = get_user_model().objects.create_user('frame', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def pages(self):
        found, skipped = every_page()
        self.assertEqual(skipped, self.SKIPPED,
                         'a route this sweep cannot reach was added')
        return found

    def test_there_are_fifty_of_them(self):
        """A reminder that this is a sweep and not a sample. Update it when
        a page is added or removed - and when one is, the rest of this class
        has already held it to the same rules."""
        self.assertEqual(len(self.pages()), 50)

    def test_every_page_renders(self):
        for name, path in self.pages():
            with self.subTest(page=name, path=path):
                response = self.client.get(path, follow=True)
                self.assertEqual(response.status_code, 200)

    def test_every_page_wears_the_one_frame(self):
        """The header, the theme picker, and who is signed in.

        The last of those was on eight pages out of fifty: an operator acting
        on a host should be able to see which account the host will record.
        """
        for name, path in self.pages():
            with self.subTest(page=name, path=path):
                body = self.client.get(path, follow=True).content.decode()
                self.assertIn('class="header-identity"', body)
                self.assertIn('theme-picker', body)
                self.assertIn('header-user', body)

    def test_every_page_can_reach_every_section(self):
        """From anywhere, the five places the console goes - which is what no
        single frame offered before."""
        for name, path in self.pages():
            with self.subTest(page=name, path=path):
                body = self.client.get(path, follow=True).content.decode()
                for section in ('Libraries', 'Tape Operations',
                                'System Console', 'About', 'Log out'):
                    self.assertIn(section, body)

    def test_every_page_says_which_version_it_is(self):
        """One footer, in one frame. It used to reach 8 pages of 50."""
        for name, path in self.pages():
            with self.subTest(page=name, path=path):
                body = self.client.get(path, follow=True).content.decode()
                self.assertIn('site-footer', body)

    #: A page in one of these sections carries that section's own links, as a
    #: second row under the shared header. The libraries pages used six
    #: breadcrumbs instead, each listing the way back to a page the header
    #: already offers - a breadcrumb says where you are, not where else you
    #: can go.
    SECTIONS = {
        '/libraries/': ('Overview', 'All libraries', 'New library',
                        'Config files', 'MHVTL status', 'Clean up orphans',
                        'Remove a library'),
        '/libraries/monitor/10/': ('All libraries', 'Config files'),
        '/libraries/setup/': ('New library', 'MHVTL status'),
        '/libraries/operator/': ('Drives', 'Tapes', 'Mount', 'LTFS', 'iSCSI'),
        '/libraries/operator/tapes/': ('Drives', 'Tapes', 'Library status'),
        '/libraries/console/': ('Logs', 'Services', 'Devices', 'Modules',
                                'Disk'),
        '/libraries/console/logs/': ('Overview', 'Services', 'Disk'),
    }

    def test_each_section_carries_its_own_links(self):
        for path, links in self.SECTIONS.items():
            body = self.client.get(path, follow=True).content.decode()
            with self.subTest(page=path):
                self.assertIn('class="section-nav"', body)
                for link in links:
                    self.assertIn(link, body)

    def test_the_section_row_says_where_you_are(self):
        """One link in it is marked, so the row is a position and not just a
        list of places."""
        for path in ('/libraries/', '/libraries/list/', '/libraries/detail/10/',
                     '/libraries/monitor/10/', '/libraries/operator/',
                     '/libraries/console/logs/'):
            body = self.client.get(path, follow=True).content.decode()
            with self.subTest(page=path):
                self.assertIn('section-link active', body)

    def test_no_page_still_draws_a_breadcrumb(self):
        """Six libraries pages did, because they had no section row. The row
        replaced them; a page with both says where you are twice."""
        for name, path in self.pages():
            with self.subTest(page=name, path=path):
                body = self.client.get(path, follow=True).content.decode()
                self.assertNotIn('class="breadcrumb"', body)


class NothingIsUnreachableTests(TestCase):
    """No page exists that nothing leads to.

    Three did: ``mhvtl_status``, ``reset_default`` and ``drive_status_detail``
    had no ``{% url %}`` in any template and no ``redirect()`` in any view.
    Being unreachable made none of them safe - ``reset_default`` deletes every
    library on the host and answers to anyone who types its address - it only
    made them impossible to find on purpose.

    This counts a link as a link whether a template writes it or a view
    redirects to it, because both are ways a person arrives.
    """

    #: Pages reached by a redirect rather than a link, which is a real way to
    #: arrive: a form posts and the view sends you on. Named here so that
    #: "nothing links to it" stays a question somebody answered.
    BY_REDIRECT_ONLY = {
        'libraries:custom_setup',                 # the setup page's own radio
        'libraries:backup_restore_test_results',  # where the test sends you
    }

    def test_every_page_has_a_way_in(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        files = (list((root / 'templates').rglob('*.html'))
                 + list(root.glob('apps/*/templates/**/*.html'))
                 + list(root.glob('apps/*/**/*.py')))
        #: Not the tests. A page named only by the test that visits it is
        #: still a page nobody can reach, and this file names all three of
        #: the ones that were.
        sources = [p.read_text(errors='replace') for p in files
                   if '/tests/' not in str(p) and not p.name.startswith('test_')]

        unreachable = []
        for name, _path in every_page()[0]:
            if name in self.BY_REDIRECT_ONLY:
                continue
            wanted = re.compile(rf"""['"]{re.escape(name)}['"]""")
            if not any(wanted.search(text) for text in sources):
                unreachable.append(name)

        self.assertEqual(unreachable, [],
                         'nothing links to these pages, so nobody can reach '
                         'them without typing the address: '
                         + ', '.join(unreachable))
