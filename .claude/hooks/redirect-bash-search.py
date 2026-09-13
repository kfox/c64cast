#!/usr/bin/env python3
"""PreToolUse(Bash) hook — deny file searches and dumps that can spill unbounded
text into the context window.

Trips on an unbounded recursive grep (`grep -r …`, or ripgrep/ag/ack, which
recurse by default) with no `-l`/`-c`/`-m` bound, and on a whole-file
`cat <file>`. A non-recursive grep, an already-bounded one, a pipe, a heredoc, a
command substitution, a redirect, and any compound beyond `cd … && cmd` pass
untouched. Every deny offers an alternative: add a bound, pipe to `head`, narrow
the path, use Read for a file, or use the Grep tool if this session has one.
"""

from __future__ import annotations

import json
import shlex
import sys

SEARCH = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}
RECURSIVE_BY_DEFAULT = {"rg", "ack", "ag"}
BOUND_LONG = {"-l", "--files-with-matches", "-L", "--files-without-match", "-c", "--count"}
BAILOUT = ("|", "<<", "$(", "`", ">", "<", ";", "\n")


def leading_argv(cmd: str) -> list[str] | None:
    """argv of the first real command, or None if we shouldn't touch it."""
    if any(tok in cmd for tok in BAILOUT):
        return None
    try:
        toks = shlex.split(cmd, comments=True)
    except ValueError:
        return None
    if not toks:
        return None
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in toks:
        if t in ("&&", "||", "&"):
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    segs = [s for s in segs if s]
    if not segs:
        return None
    if len(segs) >= 2 and segs[0][0] == "cd":
        segs = segs[1:]
    if len(segs) != 1:
        return None
    argv = segs[0]
    while argv and "=" in argv[0] and argv[0].split("=", 1)[0].isidentifier():
        argv = argv[1:]
    return argv or None


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
    return ("l" in bundles) or ("c" in bundles)


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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    argv = leading_argv(cmd)
    if not argv:
        return 0
    reason = verdict(argv)
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
