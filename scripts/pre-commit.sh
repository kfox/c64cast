#!/usr/bin/env bash
# Pre-commit gate: run the unittest suite. Invoked by .pre-commit-config.yaml.
set -euo pipefail

cd "$(dirname "$0")/.."
# PYTHONPATH=tests arms the filesystem sandbox (tests/sitecustomize.py).
PYTHONPATH=tests python -m unittest discover tests
