"""
Sphinx configuration for the MHVTL GUI User Guide.

One of four independent documentation trees:

    docs/user/       what this is - using the console
    docs/developer/  setting up, and adding to it
    docs/api/        the Python API, from the docstrings
    docs/sphinx/     the original guides, unchanged

They build separately and publish separately. The one thing they share is
the version, read from mhvtl_system/__init__.py, because a documentation set
that disagrees with itself about which release it describes is worse than no
version at all.

  English:  make html
  Arabic:   make html-ar          (sets SPHINX_LANG=ar)
  Extract:  make gettext
  Sync PO:  make update-ar
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath('../..'))


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

_build_lang = os.environ.get('SPHINX_LANG', 'en')

project   = 'MHVTL GUI User Guide'
copyright = '2026, Ahmed Abdelhaleem Ahmed'
author    = 'Ahmed Abdelhaleem Ahmed'

# The one version, from mhvtl_system/__init__.py. Read as text: building the
# docs must not need the package installed.
_version_file = Path(__file__).resolve().parents[2] / 'mhvtl_system' / '__init__.py'
_found = re.search(r"^__version__ = '(.*)'$", _version_file.read_text(), re.M)
release = _found.group(1) if _found else '0.0.0'
version = release

language = _build_lang

# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

extensions = [
    'sphinx.ext.intersphinx',
]

intersphinx_mapping = {}


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

source_suffix = {'.rst': 'restructuredtext'}
master_doc = 'index'
templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# ---------------------------------------------------------------------------
# Translation (gettext)
# ---------------------------------------------------------------------------

locale_dirs = ['locale/']
gettext_compact = False      # one .po file per page, so a translator can take one
gettext_uuid = True          # stable ids, so a reworded paragraph is the only one lost
gettext_location = True

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

html_theme = 'sphinx_rtd_theme'
html_theme_options = {
    'navigation_depth': 4,
    'collapse_navigation': False,
    'sticky_navigation': True,
    'titles_only': False,
}
html_static_path = ['_static']
html_title = f'MHVTL GUI User Guide {release}'

html_css_files = ['lang-switch.css']
html_js_files = ['lang-switch.js']

if _build_lang == 'ar':
    html_css_files = ['lang-switch.css', 'rtl.css']
    html_js_files = ['lang-switch.js', 'fix-rtl.js']
    html_title = f'MHVTL GUI User Guide {release} \u2014 \u0627\u0644\u062a\u0648\u062b\u064a\u0642 \u0627\u0644\u0639\u0631\u0628\u064a'
