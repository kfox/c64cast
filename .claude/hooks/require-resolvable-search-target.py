#!/usr/bin/env python3
"""PreToolUse(Bash) hook — deny file searches whose target cannot be resolved
statically, so they never reach the user as a permission prompt.

A bash search is read-equivalent, so before auto-approving one, Claude Code's
permission classifier has to prove the search target is not covered by a
configured `Read()` deny rule. This project denies `Read()` on `~/.ssh`,
`~/.aws`, and `~/.gnupg`, so that proof is mandatory. A target it cannot
resolve cannot be proven either way, and the classifier falls back to asking
the user — even though `Bash(grep:*)` is allowlisted. Two shapes trigger it:

  * a **relative path operand after a `cd`** — `cd /repo && grep -n foo src/x.py`
    reads "grep on 'src/x.py' after a cd would search a directory that cannot be
    determined here"; and
  * **no path operand at all** — `grep -rn foo` (or `grep -rn foo --include=*.py`,
    where the trailing flag is mistaken for the target) falls back to `.`, which
    after a `cd` is equally undeterminable.

Telling agents to pass absolute paths does not hold up under load; a hook does.
A `deny` here is handed back to the *agent*, which retries with a correct shape,
and the user sees nothing. That is the whole point: this hook exists to move a
correction from the user's terminal into the agent's loop.

Deliberately narrow. It fires only when a `cd` is present *and* a search command
in the same line has an unresolvable target. A search with absolute operands, a
search with no `cd`, and every non-search command pass untouched — as does
anything with a heredoc or command substitution, which are not worth parsing.

Wire-up (.claude/settings.json):

    {"hooks": {"PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command",
         "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/require-resolvable-search-target.py\""}
      ]}
    ]}}
"""

from __future__ import annotations

import json
import shlex
import sys

GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ack", "ag"}
FIND_LIKE = {"find", "fd", "fdfind"}
SEARCH = GREP_LIKE | FIND_LIKE
# Splitting on these is enough to find a `cd` and each command in a pipeline.
SEPARATORS = ("&&", "||", "|", ";", "&")
# Not worth parsing; never block on them.
BAILOUT = ("<<", "$(", "`")
# grep flags that take a separate value, so the next token is not a path.
GREP_VALUE_FLAGS = {"-e", "-f", "-m", "--regexp", "--file", "--max-count", "-A", "-B", "-C"}
# The subset of the above that supplies the pattern, so every bare operand is a path.
GREP_PATTERN_FLAGS = {"-e", "-f", "--regexp", "--file"}


def segments(cmd: str) -> list[tuple[list[str], bool]] | None:
    """`(argv, reads_stdin)` per command, or None if we shouldn't touch it.

    `reads_stdin` marks a segment fed by a pipe. Such a command has no
    filesystem target at all — `… | grep foo` searches its input, not `.` — so
    it can never be the unresolvable shape this hook is about.
    """
    if any(tok in cmd for tok in BAILOUT):
        return None
    try:
        toks = shlex.split(cmd, comments=True)
    except ValueError:
        return None
    if not toks:
        return None
    segs: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    piped = False
    for t in toks:
        if t in SEPARATORS:
            if cur:
                segs.append((cur, piped))
            piped = t == "|"
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append((cur, piped))
    return segs or None


def strip_env(argv: list[str]) -> list[str]:
    """Drop leading VAR=value assignments (e.g. `CI=1 grep …`)."""
    while argv and "=" in argv[0] and argv[0].split("=", 1)[0].isidentifier():
        argv = argv[1:]
    return argv


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
    segs = segments(cmd)
    if not segs:
        return None
    if not any(strip_env(argv)[:1] == ["cd"] for argv, _ in segs):
        return None  # no `cd`, so relative operands resolve against the session cwd

    for argv, reads_stdin in segs:
        if reads_stdin:
            continue  # searches its input, not the filesystem
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
