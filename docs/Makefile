# The documentation trees in this repository, built together: user api.
# Each is an independent Sphinx project - own conf.py, own search, own Arabic
# catalogue. `make -C docs strict` builds them all with warnings as errors.

TREES = user api
VENV  = ../venv

html:
	@for tree in $(TREES); do \
		echo "== $$tree"; $(MAKE) -C $$tree html >/dev/null || exit 1; \
	done
	@echo ""
	@for tree in $(TREES); do echo "  $$tree/_build/html/index.html"; done

html-ar:
	@for tree in $(TREES); do \
		echo "== $$tree (ar)"; $(MAKE) -C $$tree html-ar >/dev/null || exit 1; \
	done

# Every tree, in English, with warnings as errors. A malformed table still
# "builds successfully" and renders as a paragraph of pipes, which is how
# three of them reached the repository before this target existed.
strict:
	@for tree in $(TREES); do \
		echo "== $$tree"; $(MAKE) -C $$tree clean >/dev/null; \
		$(VENV)/bin/sphinx-build -W -b html $$tree $$tree/_build/html || exit 1; \
	done
	@echo ""
	@echo "  every tree builds with no warnings"

# Every tree, both languages, warnings shown. Slow; this is the one CI runs.
all:
	@for tree in $(TREES); do \
		echo "== $$tree"; $(MAKE) -C $$tree clean >/dev/null; \
		$(MAKE) -C $$tree html || exit 1; $(MAKE) -C $$tree html-ar || exit 1; \
	done

# Re-extract the strings and update the Arabic catalogues in every tree.
# Run it after changing any English prose, and commit the .po changes: that
# is what a translator picks up.
update-ar:
	@for tree in $(TREES); do \
		echo "== $$tree"; $(MAKE) -C $$tree update-ar >/dev/null || exit 1; \
		printf '   %s catalogues\n' "$$(find $$tree/locale -name '*.po' | wc -l)"; \
	done

clean:
	@for tree in $(TREES); do $(MAKE) -C $$tree clean; done

.PHONY: html html-ar strict all update-ar clean
