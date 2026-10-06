"""Which field a page focuses, and the global rule that used to decide.

``static/js/app.js`` focused the first editable input of the first form on
**every** page. That is right for a login box and wrong for a long form: on
the create-a-library page every earlier field is readonly and the choosers
are ``<select>``, so the first match was *Number of Drives* - and focusing it
scrolled the browser past the vendor, the tape and the library model, on load
and again on every refresh.

Removing it left the login page with nothing focused, because the `login.js`
that was supposed to handle it is loaded by no template and looks for an id
that page does not have. So the login field carries ``autofocus`` now, as an
attribute: a page that wants focus asks for it.

These read the sources rather than a browser - the browser check is in
tests/e2e/setup_form_cascade.py - because what is being pinned is that no
script hands focus out globally again.
"""
import re
from pathlib import Path

from django.template.loader import get_template

from .base import SimpleTestCase

GUI = Path(__file__).resolve().parents[3]
SCRIPTS = GUI / 'static' / 'js'


def without_comments(text):
    """The code, without the comments that explain what it used to do."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'^\s*//.*$', '', text, flags=re.M)


class FocusTests(SimpleTestCase):

    def test_no_script_focuses_a_field_on_every_page(self):
        """The rule that moved the scroll. A `.focus()` in a page's own
        script is fine; one in a shared script applies to forms it has never
        seen."""
        shared = without_comments((SCRIPTS / 'app.js').read_text())
        self.assertNotIn('.focus()', shared,
                         'app.js loads everywhere, so a focus here is a '
                         'focus on every form in the console')

    def test_the_login_field_asks_for_focus_itself(self):
        """One field on the page, and it is the one to type in."""
        source = get_template('authentication/login.html').template.source
        field = re.search(r'<input[^>]*name="pswd"[^>]*>', source)
        self.assertIsNotNone(field, 'the password field moved')
        self.assertIn('autofocus', field.group(0))

    def test_the_long_forms_ask_for_nothing(self):
        """A form read top to bottom should open at the top."""
        for name in ('libraries/brand_config.html',
                     'libraries/operator/add_drive.html'):
            with self.subTest(template=name):
                source = get_template(name).template.source
                self.assertNotIn('autofocus', source)
                self.assertNotIn('.focus()', without_comments(source))

    def test_login_js_is_not_loaded_by_anything(self):
        """It is dead: no template includes it, and the id it looks for
        (`password`) is not the one the page uses (`pass`). Pinned so that
        whoever deletes it knows it was deliberate, and so that wiring it up
        again means fixing it first."""
        loaded = [path for path in GUI.rglob('*.html')
                  if 'node_modules' not in path.parts
                  and 'login.js' in path.read_text(errors='replace')]
        self.assertEqual(loaded, [], 'login.js is included somewhere now; '
                                     'check the id it focuses still exists')
