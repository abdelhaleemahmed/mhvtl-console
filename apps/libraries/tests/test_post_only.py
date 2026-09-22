"""A URL that only answers POST must still say something to a GET.

Some URLs here are the action of a form rather than a page. Django answers a
GET on one with 405 and an empty body, which in a browser is a blank white
sheet - and a blank sheet reads as a broken console, not as a URL that was
never a page. Three of them were like that:

    /libraries/operator/tapes/adopt/   405  0 chars
    /libraries/control/50/             405  0 chars
    /libraries/iscsi/rebind/           405  0 chars

You reach one by bookmarking it, or by pressing Enter in the address bar
after submitting the form.
"""
import re
from pathlib import Path

from django.test import RequestFactory, SimpleTestCase

VIEWS = Path(__file__).resolve().parents[1]


def post_only_views():
    """(module, class, bases) for every view with post() and no get()."""
    for path in sorted(VIEWS.glob('*views*.py')):
        text = path.read_text()
        for match in re.finditer(r'^class (\w+)\(([^)]*)\):(.*?)(?=^class |\Z)',
                                 text, re.S | re.M):
            name, bases, body = match.groups()
            if 'View' not in bases:
                continue
            if (re.search(r'^\s+def post\(', body, re.M)
                    and not re.search(r'^\s+def get\(', body, re.M)):
                yield path.name, name, bases


class PostOnlyViewTests(SimpleTestCase):

    def test_every_post_only_view_answers_a_get(self):
        """Either it redirects to the page its form lives on, or it says in
        JSON that it takes POST. Never a blank page."""
        bare = [f'{module}: {name}' for module, name, bases in post_only_views()
                if 'RedirectOnGet' not in bases and 'JsonOnGet' not in bases]
        self.assertEqual(bare, [], '\n'.join(bare))

    def test_there_are_some_to_check(self):
        """A test that finds nothing to check protects nothing."""
        self.assertGreater(len(list(post_only_views())), 10)

    def test_a_form_action_sends_you_to_its_page(self):
        from apps.libraries.tape_operations_views import AdoptTapeView

        request = RequestFactory().get('/libraries/operator/tapes/adopt/')
        response = AdoptTapeView.as_view()(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/libraries/operator/tapes/')

    def test_the_library_control_form_goes_back_to_the_library(self):
        from apps.libraries.views import LibraryControlView

        request = RequestFactory().get('/libraries/control/50/')
        response = LibraryControlView.as_view()(request, library_id=50)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/libraries/detail/50/')

    def test_an_endpoint_says_what_it_takes(self):
        import json

        from apps.libraries.iscsi_views import CreateTargetAjaxView

        request = RequestFactory().get('/libraries/iscsi/ajax/target/create/')
        response = CreateTargetAjaxView.as_view()(request)
        self.assertEqual(response.status_code, 405)
        payload = json.loads(response.content)
        self.assertFalse(payload['success'])
        self.assertIn('POST', payload['error'])
