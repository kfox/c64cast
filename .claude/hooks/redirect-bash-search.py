#!/usr/bin/env python3
"""PreToolUse(Bash) hook — nudge file searches/dumps away from dumping unbounded
text into the context window.

The waste this targets is *unbounded output landing in context*, not the choice
of command. Two patterns dominate:

  * an **unbounded recursive grep** (`grep -r …`, or ripgrep/ag/ack which recurse
    by default) with no `-l`/`-c`/`-m` bound — can spill hundreds of matching
    lines into context; and
  * a whole-file **`cat <file>`** — the Read tool gives the same content with
    line numbers, `offset`/`limit`, and harness file-tracking for later edits.

It deliberately does NOT blanket-block grep. In many Claude Code sessions the
structured Grep *tool* isn't even exposed (bash grep is the sanctioned search
path), so denying every grep would remove the only mechanism available. A
non-recursive grep, or any grep already bounded with `-l`/`-c`/`-m`, passes
untouched — as does anything in a pipe (`… | grep`, `… | head`), a heredoc, a
command substitution, a redirect, or a compound beyond `cd … && cmd`.

Every deny offers an alternative that is always possible: add a bound, pipe to
`head`, narrow the path, use Read for a file — and use the Grep tool *if this
session has one*.

Wire-up (.claude/settings.json):

    {"hooks": {"PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command",
         "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/redirect-bash-search.py\""}
      ]}
    ]}}
"""

from __future__ import annotations

import json
import shlex
import sys

SEARCH = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}
# These recurse by default with no explicit -r, so a bare `rg foo` already
# walks the whole tree; plain grep/egrep/fgrep only recurse with -r/-R.
RECURSIVE_BY_DEFAULT = {"rg", "ack", "ag"}
# Flags that bound the output enough that we leave the command alone.
BOUND_LONG = {"-l", "--files-with-matches", "-L", "--files-without-match", "-c", "--count"}
# Bail out (allow) if any of these appear — too complex / legitimately shell-only.
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
    # Split on `&&` (survives shlex as a literal token); keep first segment.
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
    # Drop a leading `cd <dir>` (project convention for anchoring cwd).
    if len(segs) >= 2 and segs[0][0] == "cd":
        segs = segs[1:]
    if len(segs) != 1:
        return None  # compound beyond `cd && cmd` → leave alone
    argv = segs[0]
    # Strip leading VAR=value env assignments (e.g. CI=1 grep ...).
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
    return ("l" in bundles) or ("c" in bundles)  # -l files, -c counts


def verdict(argv: list[str]) -> str | None:
    cmd, args = argv[0], argv[1:]
    if cmd in SEARCH:
        # Need an actual search operand — `grep --version`/`rg --help` have none.
        if not [a for a in args if not a.startswith("-")]:
            return None
        # Only nudge the genuinely-wasteful shape: a recursive search with no
        # output bound. A non-recursive grep on explicit files, or any already
        # bounded with -l/-c/-m, is fine and passes through.
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
