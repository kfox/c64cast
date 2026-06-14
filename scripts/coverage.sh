#!/usr/bin/env bash
# Run the test suite under coverage.py + emit reports.
# Requires the [dev] optional dependency group (uv sync --group dev).
#
# Produces:
#   terminal        per-module coverage report
#   htmlcov/        HTML coverage report (local browsing)
#   coverage.xml    Cobertura report (CI -> Codecov coverage)
#   test-results/   JUnit XML per test class (CI -> Codecov Test Analytics)
#
# The suite runs through unittest-xml-reporting's `xmlrunner discover` -- a
# unittest.TestRunner subclass with the same discovery as `make test` -- so the
# JUnit results fall out of the same single run that gathers coverage.
set -uo pipefail

cd "$(dirname "$0")/.."

# Allow `COVERAGE="python -m coverage" make coverage` where the `coverage`
# console script isn't on PATH (some interpreter/venv combos don't install it).
COVERAGE="${COVERAGE:-coverage}"

$COVERAGE erase
# Run the suite. Don't abort on a test failure (no `set -e`): we still want
# coverage.xml + the JUnit results generated/uploaded. Re-raise the status at
# the end so `make coverage` and CI still fail the build on a red suite.
$COVERAGE run -m xmlrunner discover -s tests -o test-results
status=$?
$COVERAGE report
$COVERAGE xml
$COVERAGE html
echo
echo "HTML report: htmlcov/index.html"
exit "$status"
