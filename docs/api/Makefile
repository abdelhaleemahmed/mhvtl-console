VENV         = ../../venv
SPHINXBUILD  = $(VENV)/bin/sphinx-build
SPHINXINTL   = $(VENV)/bin/sphinx-intl
SOURCEDIR    = .
BUILDDIR     = _build

# ── English build ───────────────────────────────────────────────────────────

html:
	$(SPHINXBUILD) -b html $(SOURCEDIR) $(BUILDDIR)/html
	@echo ""
	@echo "English build complete: $(BUILDDIR)/html/index.html"

# ── Arabic build ────────────────────────────────────────────────────────────
# SPHINX_LANG=ar  → conf.py loads rtl.css and fix-rtl.js
# -D language=ar  → Sphinx uses locale/ar/LC_MESSAGES/*.po translations

html-ar:
	SPHINX_LANG=ar $(SPHINXBUILD) -b html -D language=ar \
		$(SOURCEDIR) $(BUILDDIR)/html-ar
	@echo ""
	@echo "Arabic build complete: $(BUILDDIR)/html-ar/index.html"

# ── gettext / translation pipeline ─────────────────────────────────────────

# Step 1: extract all translatable strings into .pot files
# From clean: a .pot left over from a renamed page would otherwise come back
# as an orphan .po on the next update-ar.
gettext:
	rm -rf $(BUILDDIR)/gettext
	$(SPHINXBUILD) -b gettext $(SOURCEDIR) $(BUILDDIR)/gettext
	@echo ""
	@echo "Strings extracted to $(BUILDDIR)/gettext/"

# Step 2: create or update Arabic .po files from .pot files
# Run this after 'make gettext', then edit locale/ar/LC_MESSAGES/*.po
update-ar: gettext
	$(SPHINXINTL) update -p $(BUILDDIR)/gettext -l ar
	@echo ""
	@echo "Arabic .po files updated: locale/ar/LC_MESSAGES/"

# ── Utilities ───────────────────────────────────────────────────────────────

clean:
	rm -rf $(BUILDDIR)

# Always clean before rebuilding — CSS/JS changes are NOT picked up
# by incremental builds. Use these targets, never bare 'make html'.
rebuild: clean html

rebuild-ar: clean html-ar

# Build both languages in one shot
all: clean html html-ar

.PHONY: html html-ar gettext update-ar clean rebuild rebuild-ar all
