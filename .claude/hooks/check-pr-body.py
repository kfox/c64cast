#!/usr/bin/env python3
"""PreToolUse(Bash) hook — hold a PR description to the template before `gh` sends it.

The `pr-body` workflow refuses the same descriptions, but only after the PR
exists, and by then the body is in the repository's notifications. This
denies the `gh pr create` or `gh pr edit` call instead, with the same checker
(`scripts/check_pr_body.py`), so the two cannot disagree.

A body it cannot read is denied rather than waved through: `--body "$(…)"`
reaches the hook with the substitution lifted out, and `--fill` builds the
body from commit messages that know nothing of the template. `--body-file
PATH` is the form that always reads. `gh pr edit` without a body flag changes
something else and passes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _shell  # noqa: E402

_CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "check_pr_body.py"

_BODY_FLAGS = ("--body", "-b")
_BODY_FILE_FLAGS = ("--body-file", "-F")

USE_BODY_FILE = (
    "Write the description to a file from .github/PULL_REQUEST_TEMPLATE.md and "
    "pass it with `--body-file PATH`; this hook could not read the body as given."
)


def _checker():
    spec = importlib.util.spec_from_file_location("check_pr_body", _CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _option(argv: list[str], names: tuple[str, ...]) -> str | None:
    """The last value given for any of `names`, as `--flag V` or `--flag=V`."""
    value = None
    for index, token in enumerate(argv):
        for name in names:
            if token == name and index + 1 < len(argv):
                value = argv[index + 1]
            elif token.startswith(f"{name}="):
                value = token.split("=", 1)[1]
    return value


def _verb(argv: list[str]) -> str | None:
    argv = _shell.strip_prefix(argv)
    if len(argv) >= 3 and argv[:2] == ["gh", "pr"] and argv[2] in ("create", "edit"):
        return argv[2]
    return None


def _body(command: _shell.Command, cwd: str) -> str | None:
    """The description this command sends, or None when there is none to read."""
    argv = command.argv
    inline = _option(argv, _BODY_FLAGS)
    if inline is not None:
        return None if "$(" in inline else inline

    path = _option(argv, _BODY_FILE_FLAGS)
    if path is None:
        return None
    if path == "-":
        return command.heredoc_body or None
    try:
        return (Path(cwd) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def decide(cmd: str, cwd: str) -> str | None:
    """The reason to deny `cmd`, or None to let it run."""
    reading = _shell.read(cmd)
    for command in reading.commands:
        verb = _verb(command.argv)
        if verb is None:
            continue
        sends_a_body = _option(command.argv, (*_BODY_FLAGS, *_BODY_FILE_FLAGS)) is not None
        if verb == "edit" and not sends_a_body:
            continue
        if reading.has_substitution():
            return USE_BODY_FILE
        body = _body(command, cwd)
        if body is None:
            return USE_BODY_FILE
        checker = _checker()
        found = checker.problems(body)
        if found:
            return checker.report(found)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    reason = decide(cmd, payload.get("cwd") or ".") if cmd else None
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


if __name__ == "__main__":
    sys.exit(main())
