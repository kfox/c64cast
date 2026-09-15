#!/usr/bin/env python3
"""PreToolUse(Bash) hook — force the project's test runner through `make`.

This repo runs tests via Makefile targets (`make test`, `make lint`, `make
typecheck`, `make check`) so they always hit the uv-synced project env
(`PY ?= uv run python`) regardless of whether the current shell has direnv-
activated `.venv`. A bare `python -m unittest` / `pytest` from an agent shell
silently misses that env.

Trips on a command segment that runs `unittest` or `pytest` directly —
`python -m unittest …`, `uv run python -m unittest …`, `pytest …`,
`coverage run -m pytest …`. `make test` (which runs unittest inside the recipe,
invisible to this hook) passes untouched, as does any segment whose leading
command is `make`.
"""

from __future__ import annotations

import json
import shlex
import sys

RUNNER_CMDS = {"pytest", "py.test"}
TEST_MODULES = {"unittest", "pytest"}

DENY = (
    "Run tests through the Makefile, not a raw runner — `make test` routes "
    "through `uv run` (PY ?= uv run python) so it always hits the synced "
    ".venv (avoids the 'missing cv2 locally' trap). Equivalents:\n"
    "  whole suite      -> make test\n"
    "  one module       -> make test T=tests.test_foo\n"
    "  class/method     -> make test T=tests.test_foo.ClassName.test_x\n"
    '  module w/ _fakes -> make test T="discover -s tests -p test_foo.py"\n'
    "(test_waveform and other modules that import sibling _fakes need the "
    "discover form.) Also: make lint / make typecheck / make check."
)


def _segments(cmd: str) -> list[list[str]]:
    """Split the command into argv segments on shell separators. Returns [] if
    it can't be parsed (hook then allows — never block on a parse failure)."""
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


def _strip_prefix(argv: list[str]) -> list[str]:
    """Drop leading `VAR=value` env assignments and a leading `cd <dir>`."""
    while argv and "=" in argv[0] and argv[0].split("=", 1)[0].isidentifier():
        argv = argv[1:]
    if len(argv) >= 2 and argv[0] == "cd":
        argv = argv[2:]
        argv = _strip_prefix(argv)
    return argv


def _is_raw_test_run(argv: list[str]) -> bool:
    argv = _strip_prefix(argv)
    if not argv:
        return False
    if argv[0] == "make":
        return False
    for i, tok in enumerate(argv):
        if tok in RUNNER_CMDS:
            prev = argv[i - 1] if i > 0 else ""
            if i == 0 or prev in ("run", "python", "python3", "exec", "-m"):
                return True
        if tok == "-m" and i + 1 < len(argv) and argv[i + 1] in TEST_MODULES:
            return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return 0
    if any(_is_raw_test_run(seg) for seg in _segments(cmd)):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": DENY,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
