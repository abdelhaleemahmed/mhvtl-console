"""Login enforcement and password management for the shared GUI account."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.authentication.models import DEFAULT_PASSWORD, gui_username

User = get_user_model()


class SeededAccountTests(TestCase):
    def test_migration_creates_the_shared_account(self):
        user = User.objects.get(username=gui_username())
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password(DEFAULT_PASSWORD))

    def test_password_is_hashed_not_stored_in_clear(self):
        user = User.objects.get(username=gui_username())
        self.assertNotEqual(user.password, DEFAULT_PASSWORD)
        self.assertTrue(user.password.startswith(('pbkdf2_', 'argon2', 'bcrypt', 'md5$')))


class AccountResolutionTests(TestCase):
    """An install that named its account something other than 'admin' must
    still be able to log in through the password-only form."""

    def test_sole_existing_account_is_used_when_admin_is_absent(self):
        User.objects.all().delete()
        User.objects.create_user(username='haleem', password='their-password')

        self.assertEqual(gui_username(), 'haleem')
        self.assertRedirects(
            self.client.post(reverse('authentication:login'),
                             {'pswd': 'their-password'}),
            reverse('authentication:dashboard'))

    def test_configured_account_wins_when_several_exist(self):
        User.objects.create_user(username='someone-else', password='x')

        self.assertEqual(gui_username(), 'admin')
        self.assertRedirects(
            self.client.post(reverse('authentication:login'),
                             {'pswd': DEFAULT_PASSWORD}),
            reverse('authentication:dashboard'))

    def test_inactive_accounts_are_not_chosen(self):
        User.objects.all().delete()
        User.objects.create_user(username='retired', password='x', is_active=False)

        self.assertEqual(gui_username(), 'admin')


class LoginTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.login_url = reverse('authentication:login')

    def test_correct_password_logs_in(self):
        response = self.client.post(self.login_url, {'pswd': DEFAULT_PASSWORD})
        self.assertRedirects(response, reverse('authentication:dashboard'))
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_wrong_password_is_rejected(self):
        response = self.client.post(self.login_url, {'pswd': 'not-the-password'})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_empty_password_is_rejected(self):
        response = self.client.post(self.login_url, {'pswd': ''})
        self.assertEqual(response.status_code, 401)

    def test_login_rotates_the_session_key(self):
        self.client.get(self.login_url)          # establish a session
        before = self.client.session.session_key
        self.client.post(self.login_url, {'pswd': DEFAULT_PASSWORD})
        self.assertNotEqual(before, self.client.session.session_key)

    def test_default_password_is_flagged_for_the_banner(self):
        self.client.post(self.login_url, {'pswd': DEFAULT_PASSWORD})
        self.assertTrue(self.client.session.get('using_default_password'))

    def test_logout_clears_the_session(self):
        self.client.post(self.login_url, {'pswd': DEFAULT_PASSWORD})
        self.client.get(reverse('authentication:logout'))
        self.assertFalse(self.client.session.get('mhvtl_logged_in'))

        response = self.client.get(reverse('authentication:dashboard'))
        self.assertEqual(response.status_code, 302)


class AccessControlTests(TestCase):
    """Anonymous callers must not reach anything that touches the library."""

    def setUp(self):
        self.client = Client()

    def test_anonymous_page_request_redirects_to_login(self):
        response = self.client.get(reverse('authentication:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('authentication:login'), response.url)
        self.assertIn('next=', response.url)

    def test_anonymous_library_page_redirects_to_login(self):
        response = self.client.get('/libraries/')
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('authentication:login'), response.url)

    def test_anonymous_ajax_gets_403_json_not_a_redirect(self):
        """These three endpoints had no login check of their own at all."""
        urls = [
            reverse('libraries:api_library_models', args=[1]),
            reverse('libraries:api_library_status', args=[1]),
            reverse('libraries:iscsi_status_ajax'),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.json()['success'])

    def test_ajax_path_is_detected_without_the_xhr_header(self):
        """fetch() sends no X-Requested-With, so the path has to be enough."""
        response = self.client.get(reverse('libraries:iscsi_status_ajax'))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['success'])

    def test_login_page_itself_is_reachable(self):
        self.assertEqual(self.client.get(reverse('authentication:login')).status_code, 200)

    def test_authenticated_user_reaches_the_dashboard(self):
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        self.assertEqual(self.client.get(reverse('authentication:dashboard')).status_code, 200)


class ChangePasswordTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        self.url = reverse('authentication:change_password')

    def test_password_can_be_changed_and_survives_relogin(self):
        response = self.client.post(self.url, {
            'old_password': DEFAULT_PASSWORD,
            'new_password1': 'a-much-better-secret-42',
            'new_password2': 'a-much-better-secret-42',
        })
        self.assertRedirects(response, reverse('authentication:dashboard'))

        self.client.get(reverse('authentication:logout'))
        self.assertEqual(
            self.client.post(reverse('authentication:login'),
                             {'pswd': DEFAULT_PASSWORD}).status_code,
            401, 'old password should stop working')
        self.assertRedirects(
            self.client.post(reverse('authentication:login'),
                             {'pswd': 'a-much-better-secret-42'}),
            reverse('authentication:dashboard'))

    def test_changing_password_keeps_you_logged_in(self):
        self.client.post(self.url, {
            'old_password': DEFAULT_PASSWORD,
            'new_password1': 'a-much-better-secret-42',
            'new_password2': 'a-much-better-secret-42',
        })
        self.assertEqual(self.client.get(reverse('authentication:dashboard')).status_code, 200)

    def test_changing_password_clears_the_default_password_warning(self):
        self.client.post(self.url, {
            'old_password': DEFAULT_PASSWORD,
            'new_password1': 'a-much-better-secret-42',
            'new_password2': 'a-much-better-secret-42',
        })
        self.assertFalse(self.client.session.get('using_default_password'))

    def test_wrong_old_password_is_refused(self):
        response = self.client.post(self.url, {
            'old_password': 'wrong',
            'new_password1': 'a-much-better-secret-42',
            'new_password2': 'a-much-better-secret-42',
        })
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            User.objects.get(username=gui_username()).check_password(DEFAULT_PASSWORD))

    def test_anonymous_cannot_change_the_password(self):
        self.client.get(reverse('authentication:logout'))
        response = self.client.post(self.url, {
            'old_password': DEFAULT_PASSWORD,
            'new_password1': 'attacker-chosen',
            'new_password2': 'attacker-chosen',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            User.objects.get(username=gui_username()).check_password(DEFAULT_PASSWORD))


class PageColourTests(TestCase):
    """No page may write a colour of its own.

    The pages on templates/base.html drew themselves in fixed colours - a
    white card, #667eea for the button, white at several opacities for the
    footer - so on a dark theme the card stayed white and its labels could
    not be read. Everything is a theme token now, and this says so.
    """

    STYLESHEETS = ('static/css/base.css',)
    TEMPLATES = ('templates/includes/_default_password_warning.html',
                 'templates/includes/_footer.html',
                 'templates/includes/_header.html',
                 'apps/authentication/templates/authentication/change_password.html')

    def _root(self):
        from pathlib import Path
        return Path(__file__).resolve().parents[3]

    def test_no_template_writes_a_colour(self):
        import re
        for name in self.TEMPLATES:
            with self.subTest(template=name):
                text = (self._root() / name).read_text()
                # the comments say which colours were taken out
                text = re.sub(r'{% comment %}.*?{% endcomment %}', '', text, flags=re.S)
                self.assertEqual(re.findall(r'(?<!&)#[0-9a-fA-F]{3,6}\b', text), [])
                self.assertNotIn('background:#fff', text.replace(' ', ''))

    def test_the_footer_does_not_use_the_text_colour_as_its_background(self):
        """--text is the page's text colour: nearly white on a dark theme,
        which is what made the footer white text on white."""
        css = (self._root() / 'static/css/base.css').read_text()
        rule = css[css.index('.site-footer {'):]
        rule = rule[:rule.index('}')]
        self.assertNotIn('var(--text)', rule)

    def test_form_fields_say_what_colour_their_text_is(self):
        """Without it the browser's black is used, and what is typed cannot
        be read on any dark theme."""
        css = (self._root() / 'static/css/base.css').read_text()
        rule = css[css.index('.form-input,'):]
        rule = rule[:rule.index('}')]
        self.assertIn('color: var(--text)', rule)

    def test_the_footer_shows_the_version_it_is(self):
        """It said 1.0.0 while the packages said 2.0.0."""
        from mhvtl_system import __version__
        footer = (self._root() / 'templates/includes/_footer.html').read_text()
        self.assertIn('{{ gui_version }}', footer)
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        page = self.client.get(reverse('authentication:change_password'))
        self.assertContains(page, 'Version %s' % __version__)


class DefaultPasswordWarningTests(TestCase):
    """The bar, and the x that puts it away.

    It is on every page while the console still answers to the password it
    shipped with, which on a machine one person administers is a line they
    have already read. Dismissing it is remembered in the browser; the server
    keeps sending it, so nothing about the console becomes less safe, and it
    disappears everywhere the moment the password is changed.
    """

    def setUp(self):
        self.client = Client()

    def _page(self):
        return self.client.get(reverse('authentication:dashboard'))

    def test_the_bar_is_shown_on_the_default_password(self):
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        page = self._page()
        self.assertContains(page, 'default-password-warning')
        self.assertContains(page, 'still uses the default password')

    def test_it_carries_a_dismiss_button_and_its_script(self):
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        page = self._page()
        self.assertContains(page, 'class="alert-dismiss"')
        self.assertContains(page, 'js/password-warning.js')

    def test_changing_the_password_removes_it_without_dismissing(self):
        """The dismissal lives in the browser; this is the server deciding."""
        self.client.post(reverse('authentication:login'), {'pswd': DEFAULT_PASSWORD})
        self.client.post(reverse('authentication:change_password'), {
            'old_password': DEFAULT_PASSWORD,
            'new_password1': 'a-much-better-secret-42',
            'new_password2': 'a-much-better-secret-42',
        })
        page = self._page()
        self.assertNotContains(page, 'default-password-warning')
        self.assertNotContains(page, 'js/password-warning.js')

    def test_the_script_hides_it_only_when_it_was_dismissed(self):
        """Read as text: the browser is not driven here, but the two things
        that matter are - it reads the flag, and it survives a localStorage
        that throws, which is what a private window does."""
        from pathlib import Path
        script = (Path(__file__).resolve().parents[3]
                  / 'static/js/password-warning.js').read_text()
        self.assertIn('mhvtl-default-password-dismissed', script)
        self.assertIn('localStorage.getItem', script)
        self.assertIn('localStorage.setItem', script)
        # one around the read, one around the write
        self.assertEqual(script.count('try {'), 2)
        self.assertEqual(script.count('} catch'), 2)
