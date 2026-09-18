#!/usr/bin/env python3
"""PreToolUse(Edit|Write|NotebookEdit) hook — keep edits out of the primary
checkout.

A change in this repository belongs in a worktree of its own, under
`.claude/worktrees/`, with an environment of its own. The primary checkout is
shared: another session may be standing in it, and a `git pull` there moves
HEAD out from under uncommitted work. `.claude/skills/ship/SKILL.md` step 1 is
where a change is told to enter one; this is the backstop for a session that
did not.

The decision is `ask`, not `deny`. The primary checkout has edits that belong
to it — a merge, a release, a change the user is making by hand — so the point
is to make editing it deliberate rather than impossible.

Only the file-editing tools reach here, so a write driven through Bash — `sed
-i`, a redirection, `tee` — does not. An existing worktree somewhere else is
left alone too: it is already a worktree, and where a new one may be created is
`require-worktrees-in-checkout.py`'s decision.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WORKTREE_PARTS = (".claude", "worktrees")
PATH_KEYS = ("file_path", "notebook_path")
GITDIR_PREFIX = "gitdir:"

SHARED = (
    "This path is in the repository's primary checkout, which other sessions "
    "and the user share:\n"
    "  {target}\n"
)
APPROVE = "Approve this if the edit belongs to the primary checkout itself."

ASK_FROM_PRIMARY = (
    SHARED + "A change belongs in a worktree of its own — call EnterWorktree with a "
    "`name`, then give it an environment of its own:\n"
    '  env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --all-extras\n'
    "and pass that environment to make (`make <target> "
    "UV_PROJECT_ENVIRONMENT=<worktree>/.venv`) and to git as a prefix "
    "(`UV_PROJECT_ENVIRONMENT=<worktree>/.venv git commit ...`).\n" + APPROVE
)

# EnterWorktree refuses a `name` from a session that is already in a worktree,
# so the way out of this one is to edit the worktree's own copy instead.
ASK_FROM_WORKTREE = (
    SHARED + "This session's worktree is\n  {worktree}\nMake the change there.\n" + APPROVE
)


def _linked_from(marker: Path) -> Path | None:
    """The checkout a linked worktree's `.git` file points back at, or None.

    The file reads `gitdir: <checkout>/.git/worktrees/<name>`, so the checkout
    is the parent of the `.git` in that path. The value may be relative — git
    writes one under `worktree.useRelativePaths`, and after some
    `git worktree repair` runs — and either form may go through a symlink,
    which is why it is resolved before being compared with anything.
    """
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith(GITDIR_PREFIX):
        return None
    recorded = text[len(GITDIR_PREFIX) :].split("\n", 1)[0].strip()
    admin = (marker.parent / recorded).resolve()
    for parent in admin.parents:
        if parent.name == ".git":
            return parent.parent
    return None


def _git_marker(start: Path) -> Path | None:
    """The nearest `.git` at or above `start`, or None when `start` is in no
    repository. It is a directory in a clone and a file in a linked worktree."""
    for directory in (start, *start.parents):
        marker = directory / ".git"
        if marker.exists():
            return marker
    return None


def _primary_checkout(start: Path) -> Path | None:
    """The primary checkout of the repository `start` sits in, or None when it
    sits in none. A clone carries a `.git` directory and is itself the answer;
    a linked worktree carries a `.git` file naming the clone."""
    marker = _git_marker(start)
    if marker is None:
        return None
    return marker.parent if marker.is_dir() else _linked_from(marker)


def _worktree_holding(*directories: Path) -> Path | None:
    """The first of `directories` that sits in a linked worktree, or None when
    none does.

    `EnterWorktree` refuses a `name` from a session in any linked worktree, not
    only one under `.claude/worktrees/`, which is why the location does not
    narrow this. A session's start directory and its working directory can name
    different trees, so both are asked.
    """
    for directory in directories:
        marker = _git_marker(directory)
        if marker is not None and marker.is_file():
            return marker.parent
    return None


def _in_a_worktrees_dir(target: Path, root: Path) -> bool:
    """Whether `target` sits under `root`'s `.claude/worktrees/`."""
    leading = target.relative_to(root).parts[: len(WORKTREE_PARTS)]
    return leading == WORKTREE_PARTS


def verdict(path: str, cwd: str, project_dir: str) -> str | None:
    """Why `path`, edited from `cwd`, wants a worktree first — or None when it
    is in one already, or outside this repository altogether."""
    if not path:
        return None
    working = Path(cwd or ".").resolve()
    target = (working / os.path.expanduser(path)).resolve()
    session = Path(project_dir or cwd or ".").resolve()
    root = _primary_checkout(session)
    if root is None or root not in target.parents:
        return None
    if _in_a_worktrees_dir(target, root):
        return None
    worktree = _worktree_holding(session, working)
    if worktree is not None:
        return ASK_FROM_WORKTREE.format(target=target, worktree=worktree)
    return ASK_FROM_PRIMARY.format(target=target)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = next((tool_input[key] for key in PATH_KEYS if tool_input.get(key)), "")
        reason = verdict(path, payload.get("cwd") or "", os.environ.get("CLAUDE_PROJECT_DIR", ""))
    except Exception:
        return 0  # never block on a parse failure
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
