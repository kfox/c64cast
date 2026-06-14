# One-stop targets for local dev. Mirrors what CI runs.
#
# Usage:
#   make            # = make help
#   make lint       # ruff check
#   make fmt        # ruff format
#   make test       # unittest suite (whole tree)
#   make test T=tests.test_midi_scene   # just that module/class/method
#   make coverage   # tests under coverage -> report + HTML + coverage.xml + JUnit XML
#   make typecheck  # mypy --strict on hot modules + pyright across the tree
#   make bench      # async write-pipeline benchmark
#   make check      # lint + typecheck + test (pre-PR gate)
#   make clean      # remove build artefacts

PY ?= python

.DEFAULT_GOAL := help

.PHONY: help lint fmt test coverage typecheck bench check clean schema

help:
	@echo "targets:"
	@echo "  lint       ruff check"
	@echo "  fmt        ruff format"
	@echo "  test       unittest discover (T=tests.test_foo runs just that)"
	@echo "  coverage   coverage report + HTML + coverage.xml + JUnit XML"
	@echo "  typecheck  mypy --strict (api/audio/playlist) + pyright (whole tree)"
	@echo "  bench      scripts/bench.py — async write pipeline"
	@echo "  schema     regenerate c64cast.schema.json from the config metadata"
	@echo "  check      lint + typecheck + test"
	@echo "  clean      remove build artifacts"

lint:
	ruff check .

fmt:
	ruff format .

# `make test` runs the whole suite; `make test T=tests.test_midi_scene` (or a
# class/method, e.g. T=tests.test_midi_scene.MidiSceneTest.test_x) runs just that.
test:
	$(PY) -m unittest $(if $(T),$(T),discover tests)

coverage:
	scripts/coverage.sh

typecheck:
	mypy --strict
	pyright

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
