#!/usr/bin/env python3
"""PreToolUse(Bash) hook — keep package management and type/lint checks on the
project's sanctioned entry points.

Three shapes of command silently do the wrong thing in this repo, and all three
are documented traps rather than style preferences:

  * **`pip` / `uv pip`** — CLAUDE.md: "Setup is `uv sync --all-extras` — never
    `uv pip` and never raw `pip`". `uv pip install` writes into whatever
    interpreter `UV_PYTHON`/mise happens to point at instead of resolving the
    project's dependency groups, which is the mise/`UV_PYTHON` trap described in
    CONTRIBUTING.md → "Development setup".
  * **a bare `mypy` / `pyright` / `ruff` / `black`** — the repo's gate is
    `pyright` basic tree-wide *plus* `mypy --strict` on a specific set of
    state-bearing modules (CONTRIBUTING.md → "The pre-PR gate"). Invoking one
    checker by hand on one file answers a different question than the gate does,
    and skips the pinned tool version the Makefile routes through.
  * **`python`/`python3` running project code** — the `make` targets go through
    `PY ?= uv run python` so they hit the uv-synced env from any shell. A bare
    `python3 -m c64cast …` or `python3 tests/…` in an agent shell misses it (the
    recurring "works in CI, missing cv2 locally" symptom).

Sibling hooks cover the neighboring cases: `redirect-to-make-test.py` owns raw
`unittest`/`pytest` invocations, and `redirect-bash-search.py` owns unbounded
searches and whole-file `cat`. This hook deliberately leaves alone anything the
traps don't apply to — a `python3` one-liner that doesn't import `c64cast`,
`scripts/diags/*.py` (standalone probes, run directly by shebang or under `uv
run`), and any `make` target.

A `uv run` prefix exempts the interpreter case only: `uv run python -m c64cast`
passes, because the trap there is purely which interpreter resolves. It does not
exempt the four checkers — `uv run mypy` gets the pinned version but still
answers a different question than the gate does, which is the objection.

Every deny names the exact replacement command, because the point is to redirect
the work, not to refuse it.

Wire-up (.claude/settings.json):

    {"hooks": {"PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command",
         "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/redirect-to-uv.py\""}
      ]}
    ]}}
"""

from __future__ import annotations

import json
import shlex
import sys

PIP_CMDS = {"pip", "pip3"}
# Checker -> the make target that runs it the way the pre-PR gate does.
CHECKER_TARGETS = {
    "mypy": "make typecheck",
    "pyright": "make typecheck",
    "ruff": "make lint (or `make fmt` to rewrite)",
    "black": "make fmt",
}
PYTHON_CMDS = {"python", "python3"}
# A `-m` module or path argument in one of these namespaces means project code,
# which needs the synced env. `scripts/` is exempt: the diag probes there are
# standalone entry points run both directly and under `uv run`.
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


def _segments(cmd: str) -> list[list[str]]:
    """Split into argv segments on shell separators. [] if unparseable (the hook
    then allows — never block on a parse failure)."""
    try:
        toks = shlex.split(cmd, comments=True)
    except ValueError:
        return []
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in toks:
        if t in ("&&", "||", "&", "|", ";"):
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return [s for s in segs if s]


def _peel(argv: list[str]) -> tuple[list[str], bool]:
    """Strip leading env assignments and run-wrappers off a segment.

    Returns the remaining argv and whether a uv wrapper was among the things
    stripped (which is what makes a `python` invocation acceptable)."""
    uv = False
    while argv:
        first = argv[0]
        if "=" in first and first.split("=", 1)[0].isidentifier():
            argv = argv[1:]
        elif first == "uv" and len(argv) > 1 and argv[1] == "run":
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
        return None  # the sanctioned path

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
    for seg in _segments(cmd):
        reason = verdict(seg)
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
