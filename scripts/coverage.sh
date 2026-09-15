#!/usr/bin/env bash
# Run the test suite under coverage.py + emit reports.
# Requires the [dev] optional dependency group (uv sync --group dev).
#
# Produces:
#   terminal        per-module coverage report
#   htmlcov/        HTML coverage report (local browsing)
#   coverage.xml    Cobertura report (CI -> Codecov coverage)
#   test-results/   JUnit XML per test class (CI -> Codecov Test Analytics)
set -uo pipefail

cd "$(dirname "$0")/.."

# Overridable: some interpreter/venv combos do not install the `coverage`
# console script.
COVERAGE="${COVERAGE:-coverage}"

# PYTHONPATH=tests arms the filesystem sandbox (tests/sitecustomize.py).
export PYTHONPATH=tests

$COVERAGE erase
# No `set -e`: the reports are still wanted when the suite is red, so the
# status is re-raised at the end instead.
$COVERAGE run -m xmlrunner discover -s tests -o test-results
status=$?
$COVERAGE report
$COVERAGE xml
$COVERAGE html
echo
echo "HTML report: htmlcov/index.html"
exit "$status"
