#!/usr/bin/env python3
"""PreToolUse(Bash) hook — keep package management and type/lint checks on the
project's sanctioned entry points.

Three shapes of command silently do the wrong thing in this repo:

  * **`pip` / `uv pip`** — setup is `uv sync --all-extras`. `uv pip install`
    writes into whatever interpreter `UV_PYTHON`/mise happens to point at
    instead of resolving the project's dependency groups (CONTRIBUTING.md ->
    "Development setup").
  * **a bare `mypy` / `pyright` / `ruff` / `black`** — the repo's gate is
    `pyright` basic tree-wide *plus* `mypy --strict` on a specific set of
    state-bearing modules (CONTRIBUTING.md -> "The pre-PR gate"), so one checker
    run by hand answers a different question and skips the pinned version.
  * **`python`/`python3` running project code** — the `make` targets go through
    `PY ?= uv run python`, which a bare interpreter misses.

A `uv run` prefix exempts the interpreter case only, not the four checkers.
Left alone: a `python3` one-liner that does not import `c64cast`,
`scripts/diags/*.py`, and any `make` target. Every command on the line is
read, wherever it sits in a compound — `_shell.read` finds the ones a glued
separator hides. Sibling hooks own the neighboring cases:
`redirect-to-make-test.py` for raw `unittest`/`pytest`, and
`redirect-bash-search.py` for unbounded searches and whole-file `cat`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Running `python3 <abs-path>` already puts the script's directory on
# sys.path, but a loader that does not — `spec_from_file_location`, which is
# how the tests reach a hook whose filename is no identifier — would raise
# here at import time, and a PreToolUse hook that cannot be imported is a
# hook that is silently off.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _shell  # noqa: E402

PIP_CMDS = {"pip", "pip3"}
CHECKER_TARGETS = {
    "mypy": "make typecheck",
    "pyright": "make typecheck",
    "ruff": "make lint (or `make fmt` to rewrite)",
    "black": "make fmt",
}
PYTHON_CMDS = {"python", "python3"}
PROJECT_ROOTS = ("c64cast", "tests")

PIP_DENY = (
    "Don't install with `pip`/`uv pip` — this project's setup is `uv sync "
    "--all-extras`, which resolves the dependency groups declared in "
    "pyproject.toml (`video`, `yt`, `wizard`, … plus the PEP 735 `dev` group). "
    "A `uv pip install` instead writes into whatever interpreter "
    "UV_PYTHON/mise currently points at — the trap documented in "
    "CONTRIBUTING.md -> 'Development setup'. To add a dependency, edit "
    "pyproject.toml and re-run `uv sync --all-extras`."
)

PYTHON_DENY = (
    "Run project code through `uv run`, not a bare `{cmd}` — the make targets "
    "use `PY ?= uv run python` so they hit the uv-synced .venv from any shell, "
    "whereas a bare `{cmd}` picks up whatever mise/UV_PYTHON resolves to (the "
    "'works in CI, missing cv2 locally' symptom). Use:\n"
    "  `uv run {rest}`\n"
    "  or `scripts/c64cast.sh …` to launch the app\n"
    "  or a `make` target (test / lint / typecheck / check / doctor / schema).\n"
    "Unaffected: one-liners that don't import c64cast, and scripts/diags/*.py."
)


def _peel(argv: list[str]) -> tuple[list[str], bool]:
    """Strip leading env assignments, shell keywords and run-wrappers off a
    command.

    Returns the remaining argv and whether a uv wrapper was among the things
    stripped (which is what makes a `python` invocation acceptable)."""
    uv = False
    while argv:
        argv = _shell.strip_prefix(argv)
        if not argv:
            break
        first = argv[0]
        if first == "uv" and argv[1:2] == ["run"]:
            argv, uv = argv[2:], True
        elif first in ("uv", "uvx"):
            argv, uv = argv[1:], True
        elif first in ("timeout", "cd", "env") and len(argv) > 2:
            argv = argv[2:]
        else:
            break
    return argv, uv


def _is_pip(argv: list[str]) -> bool:
    if argv[0] in PIP_CMDS:
        return True
    return any(
        tok == "-m" and i + 1 < len(argv) and argv[i + 1] in PIP_CMDS for i, tok in enumerate(argv)
    )


def _touches_project_code(args: list[str]) -> bool:
    for i, tok in enumerate(args):
        if tok == "-m" and i + 1 < len(args):
            mod = args[i + 1]
            if any(mod == r or mod.startswith(r + ".") for r in PROJECT_ROOTS):
                return True
        if tok == "-c" and i + 1 < len(args) and "c64cast" in args[i + 1]:
            return True
        if tok.endswith(".py") and any(tok.startswith(r + "/") for r in PROJECT_ROOTS):
            return True
    return False


def verdict(argv: list[str]) -> str | None:
    argv, uv = _peel(argv)
    if not argv or argv[0] == "make":
        return None

    if _is_pip(argv):
        return PIP_DENY

    target = CHECKER_TARGETS.get(argv[0])
    if target:
        return (
            f"Run `{argv[0]}` through the Makefile: **{target}**. The pre-PR "
            "gate is pyright basic tree-wide plus `mypy --strict` on the "
            "state-bearing modules (CONTRIBUTING.md -> 'The pre-PR gate'), so "
            f"a hand-rolled `{argv[0]}` on selected files answers a different "
            "question and skips the version the Makefile pins. `make check` "
            "runs the whole gate."
        )

    if argv[0] in PYTHON_CMDS and not uv and _touches_project_code(argv[1:]):
        return PYTHON_DENY.format(cmd=argv[0], rest=" ".join(argv))

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return 0
    for command in _shell.read(cmd).commands:
        reason = verdict(command.argv)
        if reason:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
