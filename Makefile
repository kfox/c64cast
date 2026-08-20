# One-stop targets for local dev. Mirrors what CI runs.
#
# Usage:
#   make            # = make help
#   make sync       # uv sync --all-extras (refresh the project env)
#   make lint       # ruff check
#   make fmt        # ruff format
#   make test       # unittest suite (whole tree, parallel across cores)
#   make test T=tests.test_midi_scene   # just that module/class/method
#   make coverage   # tests under coverage -> report + HTML + coverage.xml + JUnit XML
#   make typecheck  # mypy --strict on hot modules + pyright across the tree
#   make doctor     # offline env + config diagnostics (catches a desynced .venv)
#   make bench      # async write-pipeline benchmark
#   make check      # lint + typecheck + test (pre-PR gate)
#   make clean      # remove build artifacts
#
# Everything runs through `uv run`, so the synced project env is used regardless
# of whether direnv/mise has activated `.venv` in the current shell. That's the
# fix for "works in CI, missing cv2 locally": no target depends on a bare
# `python` that might resolve to the wrong interpreter. Override the interpreter
# with `make test PY=python` if you really want to.
PY ?= uv run python

# Local runs sync the project env first (all extras) so the interpreter always
# has the full dependency set. CI sets $CI and manages its own pinned env
# (`uv sync --frozen …`), so the prereq is skipped there — don't override CI's
# deliberate install.
SYNC := $(if $(CI),,sync)

.DEFAULT_GOAL := help

.PHONY: help sync lint fmt test coverage typecheck doctor bench check clean schema web \
        guide reference card books guide-figures reference-figures \
        reference-appendices site site-check

# Books (docs/<book>/*.md + book.toml) are rendered by Typst, which is an
# external binary rather than a Python package. The two faces (Jost*,
# Inconsolata) are OFL and committed under docs/shared/fonts/, so --font-path
# is unconditional: a PDF must not change appearance based on what fonts a
# given machine happens to have installed. --root makes the leading slash in
# the template's own paths mean the repo root.
BOOK_FONTS  := docs/shared/fonts
TYPST_FLAGS  = --root . --font-path $(BOOK_FONTS)

# Each book is a directory plus the artifact basename its book.toml declares.
# The basename is spelled in both places rather than parsed out of the TOML
# here: `clean` has to know the filenames without running Python, and a sed
# that silently matched nothing would render `docs/card/.pdf`. The two
# spellings are held together by a test instead — test_book_build.py fails if
# the Makefile does not name every book under docs/.
GUIDE_DIR   := docs/guide
GUIDE_BOOK  := c64cast-users-guide

REF_DIR     := docs/reference
REF_BOOK    := c64cast-reference-guide

CARD_DIR    := docs/card
CARD_BOOK   := c64cast-performance-card

BOOK_ARTS   := $(GUIDE_DIR)/$(GUIDE_BOOK) $(REF_DIR)/$(REF_BOOK) $(CARD_DIR)/$(CARD_BOOK)

# Markdown -> Typst -> PDF for one book: $(1) is its directory, $(2) the
# artifact basename its book.toml declares. Typst is not a Python dependency,
# so say so plainly rather than failing with "command not found".
define render-book
	@command -v typst >/dev/null 2>&1 || { \
	  echo "Rendering a book needs the typst binary, which is not a Python package."; \
	  echo "Install it with:  brew install typst"; \
	  echo "(see https://typst.app for other platforms)"; \
	  exit 1; }
	$(PY) scripts/build_book.py --book-dir $(1)
	typst compile $(TYPST_FLAGS) $(1)/$(2).typ $(1)/$(2).pdf
	@echo "wrote $(1)/$(2).pdf"
endef

help:
	@echo "targets:"
	@echo "  sync       uv sync --all-extras (refresh the project env)"
	@echo "  lint       ruff check"
	@echo "  fmt        ruff format"
	@echo "  test       unittest suite, parallel (T=tests.test_foo runs just that, serial)"
	@echo "  coverage   coverage report + HTML + coverage.xml + JUnit XML"
	@echo "  typecheck  mypy --strict (api/audio/playlist) + pyright (whole tree)"
	@echo "  doctor     offline env + config diagnostics (desynced .venv, drift)"
	@echo "  bench      scripts/bench.py — async write pipeline"
	@echo "  schema     regenerate c64cast/data/c64cast.schema.json from the config metadata"
	@echo "  web        rebuild the web console into c64cast/web/dist (needs Node)"
	@echo "  guide      render docs/guide/*.md to the User's Guide PDF (needs typst)"
	@echo "  reference  render docs/reference/*.md to the Reference Guide PDF (needs typst)"
	@echo "  card       render docs/card/*.md to the Performance Card PDF (needs typst)"
	@echo "  books      render every book"
	@echo "  site       render the documentation site into docs/_site"
	@echo "  site-check parse every site source, write nothing (what CI runs)"
	@echo "  guide-figures  redraw the guide's placeholder figures"
	@echo "  reference-figures  redraw the reference guide's diagrams"
	@echo "  reference-appendices  regenerate the reference guide's appendices A-I + index"
	@echo "  check      lint + typecheck + test"
	@echo "  clean      remove build artifacts"

sync:
	uv sync --all-extras

lint: $(SYNC)
	uv run ruff check .

fmt:
	uv run ruff format .

# `make test` runs the whole suite in parallel (unittest_parallel forks one
# process per test module — still stdlib unittest, ~3x faster since the suite
# is mostly blocked on socket/thread waits that now overlap across cores).
# `make test T=tests.test_midi_scene` (or a class/method, e.g.
# T=tests.test_midi_scene.MidiSceneTest.test_x) runs just that, serially — the
# parallel runner discovers by directory, not by dotted path.
test: $(SYNC)
	$(if $(T),$(PY) -m unittest $(T),$(PY) -m unittest_parallel -s tests)

coverage: $(SYNC)
	uv run scripts/coverage.sh

typecheck: $(SYNC)
	uv run mypy --strict
	uv run pyright

# Offline self-check: the env probe (interpreter / hard-dep import / uv.lock
# drift) plus the config diagnostics. `--skip-probe` keeps it hardware-free.
doctor: $(SYNC)
	$(PY) -m c64cast --doctor --skip-probe

bench:
	$(PY) scripts/bench.py

# Regenerate the committed JSON schema. It lives under the package (and so
# ships in the wheel) because every example config's `#:schema ../data/…`
# directive resolves against it, in a checkout and in an install alike.
# tests/test_schema.py fails if the committed file drifts from this output, so
# run this after changing any config dataclass field or overlay constructor.
schema:
	$(PY) -m c64cast --print-schema > c64cast/data/c64cast.schema.json

# Rebuild the web console. Its output is committed under c64cast/web/dist so an
# install never needs Node — which means a source change and its rebuilt bundle
# belong in the same commit, and CI reruns this and fails on a diff. `npm ci`
# rather than `npm install`: the lockfile is the pinned build, exactly as
# uv.lock is for Python. Node is not a Python package, so say so plainly rather
# than failing with "command not found".
web:
	@command -v npm >/dev/null 2>&1 || { \
	  echo "Building the web console needs Node, which is not a Python package."; \
	  echo "Install it with:  brew install node"; \
	  echo "(see https://nodejs.org for other platforms)"; \
	  exit 1; }
	cd web && npm ci --no-audit --no-fund && npm run build && npm test

# Redraw the guide's placeholder figures. Real captures saved over the same
# filenames are detected and left alone; see the script's --force-all escape.
guide-figures: $(SYNC)
	$(PY) scripts/make_guide_figures.py

# Redraw the reference guide's five diagrams. Unlike the guide's figures these
# are drawings rather than captures, so there is nothing to preserve: the
# script is the source and the PNGs are its committed output.
reference-figures: $(SYNC)
	$(PY) scripts/make_reference_diagrams.py

guide: $(SYNC)
	$(call render-book,$(GUIDE_DIR),$(GUIDE_BOOK))

reference: $(SYNC)
	$(call render-book,$(REF_DIR),$(REF_BOOK))

card: $(SYNC)
	$(call render-book,$(CARD_DIR),$(CARD_BOOK))

books: guide reference card

# The documentation site: the same Markdown as the books, plus the README and
# the standalone user docs, rendered as HTML into $(SITE_DIR). No typst and no
# project env — the builder is stdlib-only, which is why pages.yml runs it
# through `uv run --no-project` exactly as the release renders the books.
#
#   make site && python -m http.server -d $(SITE_DIR) 8000
#
# `site-check` parses every source and writes nothing; CI runs it on a pull
# request, which is the only thing that proves a book still renders before
# release day.
SITE_DIR := docs/_site

site:
	$(PY) scripts/build_site.py --out $(SITE_DIR)

site-check:
	$(PY) scripts/build_site.py --check

# Rewrite the Programmer's Reference Guide's generated appendices (A-I), its
# index and the performance card's live-target table from the config metadata.
# Unlike the books themselves this needs the project env, since it imports
# c64cast — which is exactly why it is a separate script from build_book.py,
# and why its output is committed: the release renders the PDFs with
# `uv run --no-project`.
# tests/test_reference_appendices.py fails if the committed files drift from
# this output, so run it after changing any config field, overlay, generator,
# effect, CLI flag, example config or install extra — and after renaming a
# section, which moves an anchor the index links at.
reference-appendices: $(SYNC)
	$(PY) scripts/gen_reference_appendices.py

check: lint typecheck test

clean:
	rm -rf build dist .coverage .coverage.* htmlcov coverage.xml
	rm -rf .ruff_cache .mypy_cache .pytest_cache
	rm -f $(addsuffix .typ,$(BOOK_ARTS)) $(addsuffix .pdf,$(BOOK_ARTS))
	rm -rf $(SITE_DIR)
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
