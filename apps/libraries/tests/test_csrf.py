"""CSRF protection on the AJAX endpoints.

These views used to carry @csrf_exempt, so any page on the internet could make a
logged-in operator's browser create a library, delete one, or configure an iSCSI
target. Django's test client skips CSRF checks unless asked, hence
enforce_csrf_checks=True throughout.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.authentication.models import DEFAULT_PASSWORD, gui_username

User = get_user_model()

#: One representative endpoint per view module, named by URL so a renamed route
#: fails loudly here instead of silently skipping the check.
PROTECTED_ENDPOINTS = [
    ('libraries:create_library_ajax', []),
    ('libraries:delete_library_ajax', [1]),
    ('libraries:regenerate_configs_ajax', []),
    ('libraries:cleanup_orphaned_ajax', []),
    ('libraries:iscsi_create_backstore_ajax', []),
]


def login_with_csrf(client):
    """Log in the way a browser does, carrying the token through the form.

    With enforce_csrf_checks=True the login POST needs a token of its own;
    without this the client stays anonymous and every later 403 comes from the
    login check rather than from CSRF, which would make these tests pass for
    the wrong reason.
    """
    client.get(reverse('authentication:login'))
    token = client.cookies['csrftoken'].value
    client.post(reverse('authentication:login'),
                {'pswd': DEFAULT_PASSWORD, 'csrfmiddlewaretoken': token})
    # Logging in rotates the CSRF token, so the token to use afterwards is the
    # one in the refreshed cookie, not the one the form was submitted with.
    return client.cookies['csrftoken'].value


class CsrfProtectionTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        login_with_csrf(self.client)

    def test_the_session_is_actually_logged_in(self):
        """Guards the other tests in this class: a 403 must mean CSRF, not auth."""
        response = self.client.get(reverse('libraries:list'))
        self.assertEqual(response.status_code, 200)

    def test_post_without_token_is_rejected(self):
        for name, args in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                response = self.client.post(reverse(name, args=args), {})
                self.assertEqual(
                    response.status_code, 403,
                    f'{name} accepted a POST with no CSRF token')

    def test_post_with_a_forged_token_is_rejected(self):
        for name, args in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(name, args=args), {},
                    HTTP_X_CSRFTOKEN='not-a-real-token')
                self.assertEqual(response.status_code, 403)

    def test_post_with_a_valid_token_is_not_blocked_by_csrf(self):
        """The view may still refuse the request, but not for a CSRF reason.

        This is the path the browser takes: load a page, keep the csrftoken
        cookie the server set, send it back in the X-CSRFToken header - exactly
        what static/js/csrf.js does.
        """
        self.client.get(reverse('libraries:list'))
        token = self.client.cookies['csrftoken'].value

        response = self.client.post(
            reverse('libraries:create_library_ajax'), {},
            HTTP_X_CSRFTOKEN=token)

        self.assertNotEqual(
            response.status_code, 403,
            'a request carrying the cookie token was still rejected')


class JsonBodyStillReadableTests(TestCase):
    """Enabling CSRF must not break the views that read request.body.

    To find the token Django looks in request.POST first, which consumes the
    body stream for a form-encoded request - after which request.body raises
    "You cannot access body after reading from request's data stream". It leaves
    the stream alone for other content types, and the console's JavaScript sends
    JSON, so these views keep working. This pins that down.
    """

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.token = login_with_csrf(self.client)

    def test_json_post_reaches_the_view_with_its_body_intact(self):
        response = self.client.post(
            reverse('libraries:create_library_ajax'),
            data='{"name": "csrf-probe"}',
            content_type='application/json',
            HTTP_X_CSRFTOKEN=self.token,
        )

        self.assertNotEqual(response.status_code, 403)
        # The view rejects the payload on its own terms, which is only possible
        # if it could read the body at all.
        self.assertNotIn('data stream', response.content.decode().lower())


class CsrfCookieTests(TestCase):
    """The JavaScript reads the token from the cookie, so it has to be there."""

    def test_cookie_is_set_on_a_standalone_page_with_no_form(self):
        client = Client()
        client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        client.get(reverse('libraries:list'))
        self.assertIn('csrftoken', client.cookies)

    def test_cookie_is_set_on_the_dashboard(self):
        client = Client()
        client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        client.get(reverse('authentication:dashboard'))
        self.assertIn('csrftoken', client.cookies)


class NoExemptionsRemainTests(TestCase):
    """Guard against @csrf_exempt creeping back in to silence a failing call."""

    def test_no_view_module_uses_csrf_exempt(self):
        import pathlib
        views = pathlib.Path('apps/libraries').glob('*views*.py')
        offenders = [p.name for p in views if 'csrf_exempt' in p.read_text()]
        self.assertEqual(offenders, [], f'csrf_exempt is back in: {offenders}')
