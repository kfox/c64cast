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
        guide guide-figures

# The User's Guide PDF is rendered by Typst, which is an external binary
# rather than a Python package. Its two faces (Jost*, Inconsolata) are OFL and
# committed under docs/guide/fonts/, so --font-path is unconditional: the PDF
# must not change appearance based on what fonts a given machine happens to
# have installed.
GUIDE_DIR   := docs/guide
GUIDE_TYP   := $(GUIDE_DIR)/c64cast-users-guide.typ
GUIDE_PDF   := $(GUIDE_DIR)/c64cast-users-guide.pdf
GUIDE_FONTS := $(GUIDE_DIR)/fonts
TYPST_FLAGS  = --root . --font-path $(GUIDE_FONTS)

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
	@echo "  schema     regenerate c64cast.schema.json from the config metadata"
	@echo "  guide      render docs/guide/*.md to the User's Guide PDF (needs typst)"
	@echo "  guide-figures  redraw the guide's placeholder figures"
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

# Regenerate the committed JSON schema. tests/test_schema.py fails if the
# committed file drifts from this output, so run this after changing any
# config dataclass field or overlay constructor.
schema:
	$(PY) -m c64cast --print-schema > c64cast.schema.json

# Redraw the guide's placeholder figures. Real captures saved over the same
# filenames are detected and left alone; see the script's --force-all escape.
guide-figures: $(SYNC)
	$(PY) scripts/make_guide_figures.py

# Markdown -> Typst -> PDF. Typst is not a Python dependency, so say so
# plainly rather than failing with "command not found".
guide: $(SYNC)
	@command -v typst >/dev/null 2>&1 || { \
	  echo "make guide needs the typst binary, which is not a Python package."; \
	  echo "Install it with:  brew install typst"; \
	  echo "(see https://typst.app for other platforms)"; \
	  exit 1; }
	$(PY) scripts/build_guide.py
	typst compile $(TYPST_FLAGS) $(GUIDE_TYP) $(GUIDE_PDF)
	@echo "wrote $(GUIDE_PDF)"

check: lint typecheck test

clean:
	rm -rf build dist .coverage .coverage.* htmlcov coverage.xml
	rm -rf .ruff_cache .mypy_cache .pytest_cache
	rm -f $(GUIDE_TYP) $(GUIDE_PDF)
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
