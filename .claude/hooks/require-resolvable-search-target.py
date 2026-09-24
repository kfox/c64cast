#!/usr/bin/env python3
"""PreToolUse(Bash) hook — deny file searches whose target cannot be resolved
statically, so they never reach the user as a permission prompt.

A bash search is read-equivalent, so before auto-approving one, Claude Code's
permission classifier has to prove the search target is not covered by a
configured `Read()` deny rule. This project denies `Read()` on `~/.ssh`,
`~/.aws`, and `~/.gnupg`, so that proof is mandatory. A target it cannot
resolve cannot be proven either way, and the classifier falls back to asking
the user — even though `Bash(grep:*)` is allowlisted. Two shapes trigger it:

  * a **relative path operand after a `cd`** — `cd /repo && grep -n foo src/x.py`;
  * **no path operand at all** — `grep -rn foo` (or `grep -rn foo --include=*.py`,
    where the trailing flag is mistaken for the target), which falls back to `.`.

A deny is handed back to the agent, which retries with a correct shape, and the
user sees nothing.

Fires only when a `cd` is present *and* a search command in the same line has an
unresolvable target. Every command on the line is read, wherever it sits in a
compound — `_shell.read` finds the ones a glued separator hides. A search with
absolute operands, a search with no `cd`, a search fed by a pipe, and every
non-search command pass untouched — as does anything with a command
substitution.
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

GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}
FIND_LIKE = {"find", "fd", "fdfind"}
SEARCH = GREP_LIKE | FIND_LIKE
GREP_VALUE_FLAGS = {"-e", "-f", "-m", "--regexp", "--file", "--max-count", "-A", "-B", "-C"}
GREP_PATTERN_FLAGS = {"-e", "-f", "--regexp", "--file"}


def strip_env(argv: list[str]) -> list[str]:
    """Drop leading VAR=value assignments and shell keywords (`CI=1 grep …`)."""
    return _shell.strip_prefix(argv)


def path_operands(argv: list[str]) -> list[str] | None:
    """The path operands of a search command, or None if it isn't one.

    For grep the first bare operand is the pattern unless `-e`/`-f` supplied it;
    for find every bare operand is a start path.
    """
    argv = strip_env(argv)
    if not argv or argv[0] not in SEARCH:
        return None
    cmd, args = argv[0], argv[1:]

    bare: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            if cmd in GREP_LIKE and arg in GREP_VALUE_FLAGS:
                skip_next = True
            continue
        bare.append(arg)

    if cmd in FIND_LIKE:
        return bare
    if any(a in GREP_PATTERN_FLAGS for a in args):
        return bare
    return bare[1:]


def verdict(cmd: str) -> str | None:
    """Why some search on `cmd` has a target the classifier cannot resolve,
    or None.

    A line carrying a command substitution is left alone: lifting the
    substituted command out leaves the enclosing one's operand list
    incomplete, and a deny built on a half-read operand list is the false
    deny this hook exists to avoid causing.
    """
    reading = _shell.read(cmd)
    if reading.unreadable or reading.has_substitution() or not reading.commands:
        return None
    if not any(strip_env(command.argv)[:1] == ["cd"] for command in reading.commands):
        return None

    for command in reading.commands:
        argv = command.argv
        if command.stdin_from_pipe:
            continue
        paths = path_operands(argv)
        if paths is None:
            continue
        name = strip_env(argv)[0]
        if not paths:
            return (
                f"This `{name}` has a `cd` and no path operand, so it searches `.` — a "
                "directory the permission classifier cannot determine, which makes it "
                "prompt the user instead of auto-approving. Drop the `cd`, stay in the "
                "current directory, and pass an explicit absolute path as the LAST "
                "operand: `grep -rn 'pattern' /abs/path`. Put flags BEFORE the pattern "
                "(a trailing `--include=*.py` is read as the search target)."
            )
        unresolvable = [p for p in paths if not p.startswith("/")]
        if unresolvable:
            return (
                f"This `{name}` has a `cd` and the relative operand '{unresolvable[0]}', "
                "so the permission classifier cannot determine which directory is "
                "searched and prompts the user instead of auto-approving. Drop the `cd`, "
                "stay in the current directory, and pass absolute paths: "
                f"`{name} … /abs/path`. Every operand must start with `/`."
            )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    reason = verdict((payload.get("tool_input") or {}).get("command") or "")
    if not reason:
        return 0
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
