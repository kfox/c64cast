#!/usr/bin/env python3
"""Fail when uv's project environment imports ``c64cast`` from somewhere else.

Run by the ``venv-matches-checkout`` pre-commit hook and by ``make venv-check``,
ahead of everything that invokes ``uv``. The environment's interpreter is run
directly because ``uv run`` syncs before it executes, which would perform the
reinstall this exists to catch — and with ``-I``, which drops the working
directory (the checkout itself, under pre-commit), ``PYTHONPATH`` and the user
site, so none of them can answer the lookup in the environment's place.

``UV_PROJECT_ENVIRONMENT`` is resolved the way uv resolves it: a relative value
against the project root rather than the caller's working directory
(https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_PACKAGE_ROOT_EXPR = (
    "import c64cast, pathlib; print(pathlib.Path(c64cast.__file__).resolve().parent.parent)"
)
_INTERPRETERS = ("bin/python", "Scripts/python.exe")
_RESOLVE_TIMEOUT_S = 60
_REMEDIATION = (
    "Sync this checkout into an environment of its own:\n"
    "\n"
    '  env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --all-extras\n'
    "\n"
    "and pass the same override to uv, make, pre-commit and git commands here."
)


def _checkout_root() -> pathlib.Path | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_RESOLVE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if done.returncode != 0 or not done.stdout.strip():
        return None

    return pathlib.Path(done.stdout.strip()).resolve()


def _environment_python(checkout_root: pathlib.Path, configured: str | None) -> pathlib.Path | None:
    env_dir = checkout_root / (configured or ".venv")

    for relative in _INTERPRETERS:
        candidate = env_dir / relative
        if candidate.exists() or candidate.is_symlink():
            return candidate

    return None


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Whether `path` lies under `root`, resolving its directory but not itself.

    The interpreter may be a dangling symlink, and resolving it would follow it
    out of the checkout; the directory holding it is real either way.
    """
    directory = pathlib.Path(os.path.realpath(path.parent))
    return pathlib.Path(os.path.realpath(root)) in directory.parents


def _query_package_root(python: pathlib.Path) -> tuple[bool, pathlib.Path | None]:
    """Whether the interpreter ran at all, and the `c64cast` it imports if it did."""
    try:
        done = subprocess.run(
            [str(python), "-I", "-c", _PACKAGE_ROOT_EXPR],
            capture_output=True,
            text=True,
            timeout=_RESOLVE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None

    if done.returncode != 0 or not done.stdout.strip():
        return True, None

    return True, pathlib.Path(done.stdout.strip()).resolve()


def _report(python: pathlib.Path, imported: pathlib.Path, checkout: pathlib.Path) -> None:
    print(
        f"the project environment does not import this checkout\n"
        f"\n"
        f"  interpreter:   {python}\n"
        f"  it imports:    {imported}\n"
        f"  this checkout: {checkout}\n"
        f"\n"
        f"A uv command run here reinstalls this checkout into that environment,\n"
        f"so whatever relies on it imports this source instead.\n"
        f"\n"
        f"{_REMEDIATION}",
        file=sys.stderr,
    )


def _report_unrunnable(python: pathlib.Path, checkout: pathlib.Path) -> None:
    print(
        f"the project environment is outside this checkout and its interpreter\n"
        f"does not run\n"
        f"\n"
        f"  interpreter:   {python}\n"
        f"  this checkout: {checkout}\n"
        f"\n"
        f"What it imports cannot be established, and a uv command run here would\n"
        f"rebuild that environment from this source.\n"
        f"\n"
        f"{_REMEDIATION}",
        file=sys.stderr,
    )


def main() -> int:
    checkout_root = _checkout_root()
    if checkout_root is None:
        return 0

    python = _environment_python(checkout_root, os.environ.get("UV_PROJECT_ENVIRONMENT"))
    if python is None:
        return 0

    ran, imported = _query_package_root(python)
    if not ran:
        if _inside(python, checkout_root):
            return 0

        _report_unrunnable(python, checkout_root)
        return 1

    if imported is None or imported == checkout_root:
        return 0

    _report(python, imported, checkout_root)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
