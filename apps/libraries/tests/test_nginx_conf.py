"""packaging/rpm/mhvtl-gui-nginx.conf, on the points that have broken before.

A mistake here shows up only after an upgrade, on someone else's browser, and
looks like the console being wrong rather than stale - which is exactly what
happened: the tape tiles' bars were missing for a returning visitor while
every check on the server said they were there.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

GUI = Path(__file__).resolve().parents[3]
CONF = GUI / 'packaging' / 'rpm' / 'mhvtl-gui-nginx.conf'


def static_block() -> str:
    """The directives in `location /static/`, without the comments - which
    explain what was taken out and so contain the words being checked for."""
    text = CONF.read_text()
    start = text.index('location /static/')
    block = text[start:text.index('}', start)]
    return '\n'.join(line for line in block.splitlines()
                     if not line.strip().startswith('#'))


class StaticCachingTests(SimpleTestCase):

    def test_static_files_are_revalidated(self):
        """The names carry no content hash - css/mhvtl-console.css is that
        file at every version - so a browser told to cache it cannot tell it
        has changed. It must ask."""
        self.assertIn('no-cache', static_block())

    def test_static_files_are_not_immutable(self):
        """`immutable` means "never ask again". With unhashed names that
        serves the previous release's stylesheet and scripts to everyone who
        visited before the upgrade, for as long as the max-age says."""
        block = static_block()
        self.assertNotIn('immutable', block)
        self.assertIsNone(re.search(r'expires\s+\d+[dhmy]', block),
                          'a long expiry on unhashed files is the same trap')

    def test_the_whole_file_still_parses_as_one_server_block(self):
        text = CONF.read_text()
        self.assertEqual(text.count('{'), text.count('}'))
