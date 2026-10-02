"""The console's identity, in one place.

The footer said 1.0.0 while the packages said 2.0.0, because each was
written by hand where it was needed. packaging/build.sh reads this file.

WHY A CONSTANT AND NOT importlib.metadata
-----------------------------------------
Because distribution metadata is a function of the working directory here,
and a stale copy lies with full confidence. Asked for `mhvtl-gui` it answers
version 1.1.1 and licence GPLv3 - from the installed console as readily as
from the development venv - because an untracked mhvtl_gui.egg-info/ left
over from 1.1.1 sits in the checkout and wins over the real
mhvtl_gui-3.0.0.dist-info in the venv whenever the checkout is the cwd.

Nothing here imports Django or reads a setting, so the command line can ask
before anything is configured. The values are rendered by
services/about/, which is the only thing that composes them - see
docs/sphinx/guides/plan-about.rst.

pyproject.toml carries the same four facts, because packaging metadata has to
be declarative. test_about.py holds the two files to each other.
"""
__version__ = '3.1.0'
__author__ = 'Ahmed Abdelhaleem Ahmed'
__email__ = 'ahmedhal@gmail.com'
__licence__ = 'GPL-2.0-only'
__url__ = 'https://github.com/abdelhaleemahmed/mhvtl-console'

#: Where the published documentation lives. Built from a tag by
#: packaging/docs-site.sh and served from the repository's gh-pages branch.
DOCS_USER = 'https://abdelhaleemahmed.github.io/mhvtl-console/user/'
DOCS_API = 'https://abdelhaleemahmed.github.io/mhvtl-console/api/'
