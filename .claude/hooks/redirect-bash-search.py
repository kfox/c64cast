#!/usr/bin/env python3
"""PreToolUse(Bash) hook — deny file searches and dumps that can spill unbounded
text into the context window.

Trips on an unbounded recursive grep (`grep -r …`, or ripgrep/ag/ack, which
recurse by default) with no `-l`/`-c`/`-m`/`-q` bound, and on a whole-file
`cat <file>`. Every command on the line is read, wherever it sits in a
compound — `_shell.read` finds the ones a glued separator hides. A
non-recursive grep, an already-bounded one, a command whose output goes to a
pipe or a file, and a line carrying a command substitution pass untouched.
Every deny offers an alternative: add a bound, pipe to `head`, narrow the
path, use Read for a file, or use the Grep tool if this session has one.
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

SEARCH = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}
RECURSIVE_BY_DEFAULT = {"rg", "ack", "ag"}
BOUND_LONG = {
    "-l",
    "--files-with-matches",
    "-L",
    "--files-without-match",
    "-c",
    "--count",
    "--quiet",
    "--silent",
}


def _short_bundles(args: list[str]) -> str:
    """Concatenated letters of short-option bundles, e.g. ['-rn','-i'] -> 'rni'."""
    return "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))


def _is_recursive(cmd: str, args: list[str]) -> bool:
    if cmd in RECURSIVE_BY_DEFAULT:
        return True
    bundles = _short_bundles(args)
    return ("--recursive" in args) or ("r" in bundles) or ("R" in bundles)


def _is_bounded(args: list[str]) -> bool:
    if any(a in BOUND_LONG for a in args):
        return True
    if any(a.startswith("-m") or a.startswith("--max-count") for a in args):
        return True
    bundles = _short_bundles(args)
    return any(letter in bundles for letter in "lcq")


def verdict(argv: list[str]) -> str | None:
    cmd, args = argv[0], argv[1:]
    if cmd in SEARCH:
        if not [a for a in args if not a.startswith("-")]:
            return None
        if _is_recursive(cmd, args) and not _is_bounded(args):
            return (
                f"This recursive `{cmd}` has no output bound and can dump hundreds "
                "of matching lines into the context window. Bound it: `-l` (just "
                "file names), `-c` (counts), `-m N` (max matches), or `| head -N`; "
                "narrow the path; or use the Grep tool if this session exposes one. "
                "A non-recursive or already-bounded grep won't trip this check."
            )
        return None
    if cmd == "cat":
        non_flag = [a for a in args if not a.startswith("-")]
        if len(non_flag) == 1:
            return (
                "Use the Read tool instead of `cat <file>`. It adds line numbers, "
                "takes `offset`/`limit` so you can read just the region you need, "
                "and lets the harness track the file for later edits. (Multiple "
                "files, `cat … | …`, and heredocs are unaffected.)"
            )
    return None


def line_verdict(cmd: str) -> str | None:
    """Why some command on `cmd` would spill unbounded text, or None.

    Output that goes to a pipe or a file never reaches the context window,
    however much of it there is, so those commands are passed over. A line
    carrying a command substitution is left alone entirely: lifting the
    substituted command out leaves the enclosing one's operands incomplete,
    and this hook's costly direction is the false deny — it nudges, and a
    wrong nudge spends a round trip on a command that was fine.
    """
    reading = _shell.read(cmd)
    if reading.unreadable or reading.has_substitution():
        return None
    for command in reading.commands:
        if command.stdout_to_pipe or command.stdout_to_file:
            continue
        argv = _shell.strip_prefix(command.argv)
        if not argv:
            continue
        reason = verdict(argv)
        if reason:
            return reason
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    reason = line_verdict((payload.get("tool_input") or {}).get("command") or "")
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
