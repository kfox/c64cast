PY ?= uv run python

# Arms the test suite's filesystem sandbox: `site` imports
# tests/sitecustomize.py from here at interpreter startup, in
# unittest_parallel's worker processes as well as in the parent.
TEST_ENV := PYTHONPATH=tests

# CI manages its own pinned env (`uv sync --frozen …`), so the prereq is
# skipped there.
SYNC := $(if $(CI),,sync)

# `uv run` syncs before it executes, so a target that runs uv without
# refreshing the env still needs the guard. $(SYNC) already carries it.
GUARD := $(if $(CI),,venv-check)

HAS_PARALLEL := $(shell command -v parallel 2>/dev/null)

.DEFAULT_GOAL := help

.PHONY: help sync venv-check lint fmt test coverage typecheck doctor bench check preflight clean schema web web-check \
	mutation-ready \
	mutation-check \
        guide reference card books guide-figures reference-figures \
        reference-appendices site site-check

# `--font-path` is unconditional so a PDF does not change appearance with the
# fonts a machine happens to have installed; `--root .` makes the leading slash
# in the template's own paths mean the repo root.
BOOK_FONTS  := docs/shared/fonts
TYPST_FLAGS  = --root . --font-path $(BOOK_FONTS)

# Each basename is spelled here as well as in the book's own book.toml;
# tests/test_book_build.py fails if the Makefile does not name every book
# under docs/.
GUIDE_DIR   := docs/guide
GUIDE_BOOK  := c64cast-users-guide

REF_DIR     := docs/reference
REF_BOOK    := c64cast-reference-guide

CARD_DIR    := docs/card
CARD_BOOK   := c64cast-performance-card

BOOK_ARTS   := $(GUIDE_DIR)/$(GUIDE_BOOK) $(REF_DIR)/$(REF_BOOK) $(CARD_DIR)/$(CARD_BOOK)

# Markdown -> Typst -> PDF for one book: $(1) is its directory, $(2) the
# artifact basename its book.toml declares.
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

PYRIGHT_PLATFORMS := Linux Darwin Windows
MYPY_PLATFORMS    := linux darwin win32

# Run a type checker once per platform, in parallel, failing at the first
# failure: $(1) = display name for the echo line, $(2) = the `uv run ...`
# command up to and including its platform flag (no trailing value — that is
# appended per job), $(3) = that tool's platform list.
define check-platforms
	@if [ -n "$(HAS_PARALLEL)" ]; then \
	  echo "$(1): checking $(3) with GNU parallel..."; \
	  parallel --halt now,fail=1 --tagstring '[{}]' -j $(words $(3)) $(2) {} ::: $(3); \
	else \
	  echo "$(1): checking $(3) with xargs (brew install parallel for tagged/interleaved output)..."; \
	  printf '%s\n' $(3) | xargs -n 1 -P $(words $(3)) -I {} bash -c \
	    '$(2) "$$1" 2>&1 | sed "s/^/[$$1] /"; exit $${PIPESTATUS[0]}' _ {}; \
	fi
endef

help:
	@echo "targets:"
	@echo "  sync       uv sync --all-extras (refresh the project env)"
	@echo "  lint       ruff check + ruff format --check"
	@echo "  fmt        ruff format"
	@echo "  mutation-ready  hash-based .pyc invalidation, so a mutation pass cannot read stale bytecode"
	@echo "  mutation-check  verify the tree is still armed (a clean/worktree/sync un-arms it silently)"
	@echo "  test       unittest suite, parallel (T=tests.test_foo runs just that, serial)"
	@echo "  coverage   coverage report + HTML + coverage.xml + JUnit XML"
	@echo "  typecheck  mypy --strict (api/audio/playlist) + pyright (whole tree)"
	@echo "  doctor     offline env + config diagnostics (desynced .venv, drift)"
	@echo "  bench      scripts/bench.py — async write pipeline"
	@echo "  schema     regenerate c64cast/data/c64cast.schema.json from the config metadata"
	@echo "  web        rebuild the web console into c64cast/web/dist (needs Node)"
	@echo "  web-check  the committed bundle matches a fresh build, emitted files and all (what CI runs)"
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
	@echo "  preflight  lint + hygiene hooks + test + Linux/Darwin/Windows type-checks + docs/web drift + docs search (all of CI but coverage and the OS/Python matrix)"
	@echo "  clean      remove build artifacts"

venv-check:
	@scripts/check_venv_target.py

sync: venv-check
	uv sync --all-extras

lint: $(SYNC)
	uv run ruff check .
	uv run ruff format --check .

fmt: $(GUARD)
	uv run ruff format .

# A bytecode sweep rooted at `.` reaches .venv's dependency bytecode and any
# nested checkout under .claude/worktrees/. `.claude/hooks` is here because
# the hook tests import a hook by path, which caches its bytecode like any
# other import.
SOURCE_ROOTS := c64cast tests scripts .claude/hooks

# Default .pyc validation keys on the source's mtime truncated to whole
# seconds plus its size, so a mutation applied and reverted within one second
# runs stale bytecode and reports green. `-f` is required: compileall otherwise
# skips any file whose timestamp cache is still valid.
mutation-ready: $(SYNC)
	$(PY) -m compileall -q -f --invalidation-mode checked-hash $(SOURCE_ROOTS)
	$(PY) scripts/check_hash_based_pycs.py $(SOURCE_ROOTS)

# Arming is not durable: a `make clean`, a fresh worktree, a uv sync that moves
# the Python minor, or `make test PY=python` all un-arm the tree silently. Run
# this at the moment a mutation proof's green is about to be believed.
mutation-check: $(SYNC)
	$(PY) scripts/check_hash_based_pycs.py $(SOURCE_ROOTS)

test: $(SYNC)
	$(if $(T),$(TEST_ENV) $(PY) -m unittest $(T),$(TEST_ENV) $(PY) -m unittest_parallel -s tests)

coverage: $(SYNC)
	uv run scripts/coverage.sh

typecheck: $(SYNC)
	uv run mypy --strict
	uv run pyright

doctor: $(SYNC)
	$(PY) -m c64cast --doctor --skip-probe

bench: $(GUARD)
	$(PY) scripts/bench.py

# tests/test_schema.py fails if the committed schema drifts from this output,
# so run this after changing any config dataclass field or overlay constructor.
schema: $(GUARD)
	$(PY) -m c64cast --print-schema > c64cast/data/c64cast.schema.json

# Node is not a Python package, so the steps needing it say where to get it
# rather than failing as "command not found": $(1) is the binary, $(2) names
# what wanted it.
define require-node
	@command -v $(1) >/dev/null 2>&1 || { \
	  echo "$(2) needs $(1), which is not a Python package."; \
	  echo "Install it with:  brew install node"; \
	  echo "(see https://nodejs.org for other platforms)"; \
	  exit 1; }
endef

# c64cast/web/dist is committed build output, so a source change and its
# rebuilt bundle belong in the same commit — CI reruns this and fails on a diff.
web:
	$(call require-node,npm,Building the web console)
	cd web && npm ci --no-audit --no-fund && npm run build && npm test

# `git diff` alone is blind to a path the build newly emitted and nobody
# committed, because an untracked file is not a diff: a source change that adds a
# dynamic import emits a chunk, `git commit -a` stages only the tracked half, and
# the diff then reports a bundle that is missing a file its own app.js imports.
# Both halves, or this gate passes a console that 404s on load.
web-check:
	git diff --exit-code -- c64cast/web/dist
	@stray=$$(git ls-files --others --exclude-standard -- c64cast/web/dist); \
	  [ -z "$$stray" ] || { \
	    echo "The build emitted files under c64cast/web/dist that are not committed:"; \
	    echo "$$stray" | sed 's/^/  /'; \
	    echo "Stage them: git add c64cast/web/dist"; \
	    exit 1; }

# Real captures saved over the same filenames are left alone; the script's
# --force-all is the escape.
guide-figures: $(SYNC)
	$(PY) scripts/make_guide_figures.py

reference-figures: $(SYNC)
	$(PY) scripts/make_reference_diagrams.py

guide: $(SYNC)
	$(call render-book,$(GUIDE_DIR),$(GUIDE_BOOK))

reference: $(SYNC)
	$(call render-book,$(REF_DIR),$(REF_BOOK))

card: $(SYNC)
	$(call render-book,$(CARD_DIR),$(CARD_BOOK))

books: guide reference card

SITE_DIR := docs/_site

site: $(GUARD)
	$(PY) scripts/build_site.py --out $(SITE_DIR)

site-check: $(GUARD)
	$(PY) scripts/build_site.py --check

# tests/test_reference_appendices.py fails if the committed appendices drift
# from this output, so run it after changing any config field, overlay,
# generator, effect, CLI flag, example config or install extra — and after
# renaming a section, which moves an anchor the index links at.
reference-appendices: $(SYNC)
	$(PY) scripts/gen_reference_appendices.py

check: lint typecheck test

preflight: lint test
	SKIP=ruff,ruff-format,pyright,unittest uv run --locked pre-commit run --all-files
	$(call check-platforms,pyright,uv run pyright --pythonplatform,$(PYRIGHT_PLATFORMS))
	$(call check-platforms,mypy --strict,uv run mypy --strict --platform,$(MYPY_PLATFORMS))
	@for book in docs/*/book.toml; do \
	  $(PY) scripts/build_book.py --book-dir "$$(dirname "$$book")" --check || exit 1; \
	done
	$(MAKE) site-check
	$(call require-node,node,The docs search test)
	node --test docs/shared/search.test.mjs
	$(MAKE) web
	$(MAKE) web-check

clean:
	rm -rf build dist .coverage .coverage.* htmlcov coverage.xml
	rm -rf .ruff_cache .mypy_cache .pytest_cache
	rm -rf __pycache__ *.egg-info
	rm -f $(addsuffix .typ,$(BOOK_ARTS)) $(addsuffix .pdf,$(BOOK_ARTS))
	rm -rf $(SITE_DIR)
	find $(SOURCE_ROOTS) -type d -name '__pycache__' -prune -exec rm -rf {} +
