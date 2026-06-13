#!/usr/bin/env bash
# Pre-commit gate: run the unittest suite. Invoked by .pre-commit-config.yaml.
# Ruff is run as a separate hook in that file.
set -euo pipefail

cd "$(dirname "$0")/.."
python -m unittest discover tests
