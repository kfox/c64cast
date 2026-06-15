# One-stop targets for local dev. Mirrors what CI runs.
#
# Usage:
#   make            # = make help
#   make sync       # uv sync --all-extras (refresh the project env)
#   make lint       # ruff check
#   make fmt        # ruff format
#   make test       # unittest suite (whole tree)
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

.PHONY: help sync lint fmt test coverage typecheck doctor bench check clean schema

help:
	@echo "targets:"
	@echo "  sync       uv sync --all-extras (refresh the project env)"
	@echo "  lint       ruff check"
	@echo "  fmt        ruff format"
	@echo "  test       unittest discover (T=tests.test_foo runs just that)"
	@echo "  coverage   coverage report + HTML + coverage.xml + JUnit XML"
	@echo "  typecheck  mypy --strict (api/audio/playlist) + pyright (whole tree)"
	@echo "  doctor     offline env + config diagnostics (desynced .venv, drift)"
	@echo "  bench      scripts/bench.py — async write pipeline"
	@echo "  schema     regenerate c64cast.schema.json from the config metadata"
	@echo "  check      lint + typecheck + test"
	@echo "  clean      remove build artifacts"

sync:
	uv sync --all-extras

lint: $(SYNC)
	uv run ruff check .

fmt:
	uv run ruff format .

# `make test` runs the whole suite; `make test T=tests.test_midi_scene` (or a
# class/method, e.g. T=tests.test_midi_scene.MidiSceneTest.test_x) runs just that.
test: $(SYNC)
	$(PY) -m unittest $(if $(T),$(T),discover tests)

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

check: lint typecheck test

clean:
	rm -rf build dist .coverage .coverage.* htmlcov coverage.xml
	rm -rf .ruff_cache .mypy_cache .pytest_cache
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
