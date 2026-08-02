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
#   make clean      # remove build artefacts
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

.PHONY: help sync lint fmt test coverage typecheck doctor bench check clean schema \
        guide reference books guide-figures reference-appendices

# Books (docs/<book>/*.md + book.toml) are rendered by Typst, which is an
# external binary rather than a Python package. The two faces (Jost*,
# Inconsolata) are OFL and committed under docs/shared/fonts/, so --font-path
# is unconditional: a PDF must not change appearance based on what fonts a
# given machine happens to have installed. --root makes the leading slash in
# the template's own paths mean the repo root.
BOOK_FONTS  := docs/shared/fonts
TYPST_FLAGS  = --root . --font-path $(BOOK_FONTS)

GUIDE_DIR   := docs/guide
GUIDE_TYP   := $(GUIDE_DIR)/c64cast-users-guide.typ
GUIDE_PDF   := $(GUIDE_DIR)/c64cast-users-guide.pdf

REF_DIR     := docs/reference
REF_TYP     := $(REF_DIR)/c64cast-reference-guide.typ
REF_PDF     := $(REF_DIR)/c64cast-reference-guide.pdf

# Markdown -> Typst -> PDF for one book: $(1) is its directory, $(2) the
# artefact basename its book.toml declares. Typst is not a Python dependency,
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
	@echo "  guide      render docs/guide/*.md to the User's Guide PDF (needs typst)"
	@echo "  reference  render docs/reference/*.md to the Reference Guide PDF (needs typst)"
	@echo "  books      render every book"
	@echo "  guide-figures  redraw the guide's placeholder figures"
	@echo "  reference-appendices  regenerate the reference guide's appendices A-H"
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

# Redraw the guide's placeholder figures. Real captures saved over the same
# filenames are detected and left alone; see the script's --force-all escape.
guide-figures: $(SYNC)
	$(PY) scripts/make_guide_figures.py

guide: $(SYNC)
	$(call render-book,$(GUIDE_DIR),c64cast-users-guide)

reference: $(SYNC)
	$(call render-book,$(REF_DIR),c64cast-reference-guide)

books: guide reference

# Rewrite the Programmer's Reference Guide's generated appendices (A-H) and the
# performance card's live-target table from the config metadata. Unlike the
# books themselves this needs the project env, since it imports c64cast — which
# is exactly why it is a separate script from build_book.py, and why its output
# is committed: the release renders the PDFs with `uv run --no-project`.
# tests/test_reference_appendices.py fails if the committed files drift from
# this output, so run it after changing any config field, overlay, generator,
# effect, CLI flag or example config.
reference-appendices: $(SYNC)
	$(PY) scripts/gen_reference_appendices.py

check: lint typecheck test

clean:
	rm -rf build dist .coverage .coverage.* htmlcov coverage.xml
	rm -rf .ruff_cache .mypy_cache .pytest_cache
	rm -f $(GUIDE_TYP) $(GUIDE_PDF) $(REF_TYP) $(REF_PDF)
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
