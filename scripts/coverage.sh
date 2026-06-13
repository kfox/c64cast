#!/usr/bin/env bash
# Run the test suite under coverage.py + print a per-module report.
# Requires the [dev] optional dependency group (pip install -e .[dev]).
set -euo pipefail

cd "$(dirname "$0")/.."
coverage erase
coverage run -m unittest discover tests
coverage report
coverage html
echo
echo "HTML report: htmlcov/index.html"
