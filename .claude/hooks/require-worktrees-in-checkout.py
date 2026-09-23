#!/usr/bin/env python3
"""PreToolUse(Bash) hook — keep git worktrees under `.claude/worktrees/`.

Claude Code creates a worktree at `<repo>/.claude/worktrees/<name>`, and this
repo is set up for that: `.gitignore` excludes `.claude/*` so ruff skips a
nested checkout, `make clean` sweeps the roots it names rather than the tree,
and tests/test_docs_links.py prunes any directory carrying a `.git`. A worktree
anywhere else — `$TMPDIR`, `~/src`, `~/.claude/worktrees` — is outside all of
that.

Trips on a `git worktree add` or `git worktree move` whose destination does not
land under the `.claude/worktrees/` of a checkout. A checkout is a directory
carrying a `.git` — the same marker `tests/test_docs_links.py`'s
`_nested_checkout` prunes on, a file in a linked worktree and a directory in a
clone — so the rule holds from inside a worktree too. The home directory is
excluded even when it carries one, because `~/.claude/worktrees` is Claude
Code's own location rather than any checkout's.

A path that is not there decides no, so a destination the hook cannot confirm
is refused rather than waved through.

A command handed to another shell is read as commands, not as an argument:
`bash -c`, `eval`, and a heredoc whose reader is a shell. A heredoc read by
anything else stays prose — a review record is written through one and quotes
the destinations it probed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Running `python3 <abs-path>` already puts the script's directory on
# sys.path, but a loader that does not — `spec_from_file_location`, which is
# how the tests reach a hook whose filename is no identifier — would raise
# here at import time, and a PreToolUse hook that cannot be imported is a
# hook that is silently off.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _shell  # noqa: E402

WORKTREE_DIR = Path(".claude") / "worktrees"

# `git worktree add [-f] [--detach] [--lock [--reason <string>]] [--orphan]
#  [(-b | -B) <new-branch>] <path> [<commit-ish>]` — only these take a value,
# so once they and their values are dropped the first token left is the
# destination.
VALUE_LONG_FLAGS = {"--reason"}
VALUE_SHORT_FLAGS = "bB"
GIT_NAMES = {"git", "git.exe"}
HELP_FLAGS = {"-h", "--help"}
SHELLS = {"bash", "dash", "ksh", "sh", "zsh"}
SUBSTITUTION = "`$()"

DENY = (
    "Put the worktree under this checkout's `.claude/worktrees/`:\n"
    "  git worktree add -b <branch> {allowed}/<name> origin/main\n"
    "(or call EnterWorktree with a `name`, which already lands there).\n"
    "Refused destination: {target}"
)

UNREADABLE = (
    "This command names `git worktree add` but a quote it opens is never "
    "closed, so the destination cannot be read. Close the quote, or split the "
    "command.\n"
    "Unreadable: {text}"
)


def _names_a_worktree_command(text: str) -> bool:
    """Whether unreadable text mentions the command this hook decides on. Read
    on text no lexer could take apart, so it matches words rather than tokens."""
    return "git" in text and "worktree" in text and ("add" in text or "move" in text)


def _after_directory_change(
    argv: list[str], base: Path, stack: list[Path]
) -> tuple[Path, list[str]]:
    """`base` after the segment's leading directory change, and the segment
    without it. `pushd` and `popd` move as `cd` does, through a stack; only the
    builtin and its operand are consumed, so whatever follows is still read."""
    name = argv[0]
    if name == "cd":
        return (base / os.path.expanduser(argv[1]) if argv[1:] else Path.home()), argv[2:]
    if name == "pushd":
        stack.append(base)
        return (base / os.path.expanduser(argv[1]) if argv[1:] else base), argv[2:]
    if name == "popd":
        return (stack.pop() if stack else base), argv[1:]
    return base, argv


def _chdir(argv: list[str]) -> str:
    """The directory a `git -C` runs from, composed left to right — a relative
    destination resolves against it, not against the shell's directory."""
    base = ""
    rest = list(argv)
    while rest:
        if rest.pop(0) != "-C" or not rest:
            continue
        value = rest.pop(0)
        base = str(Path(base) / value) if base else value
    return base


def _shell_payload(args: list[str]) -> list[str]:
    """The command a shell was handed, which follows the first short cluster
    carrying a `c` — `-c` alone, or `-lc` and `-ec` with company."""
    for i, token in enumerate(args):
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            return args[i + 1 : i + 2]
    return []


def _inner_commands(command: _shell.Command) -> list[str]:
    """The command strings a command hands to another shell, which have to be
    read as commands rather than as arguments. The shell is looked for
    anywhere in the argv, because a wrapper in front of it — `env`, `nohup`,
    `timeout` — keeps it out of `argv[0]`.

    A heredoc body is one of them when the command reading it is a shell.
    `bash <<'EOF'` is handed a script; `record <<'EOF'` is handed prose, and
    a review record quotes the destinations it probed — which is why only the
    command that opened the heredoc can say which of the two it is.
    """
    argv = command.argv
    for i, token in enumerate(argv):
        name = Path(token).name
        if name == "eval":
            return [" ".join(argv[i + 1 :])]
        if name in SHELLS:
            payload = _shell_payload(argv[i + 1 :])
            return [*payload, command.heredoc_body] if command.heredoc_body else payload
    return []


def _names_git(token: str) -> bool:
    """Whether `token` is git, under any command substitution glued to it.
    shlex splits `$(` off the word that follows it but leaves a backtick
    attached, so `` `git `` has to reach the same answer as `$(git`."""
    return Path(token.strip(SUBSTITUTION)).name in GIT_NAMES


def _worktree_args(argv: list[str]) -> tuple[str, list[str]] | None:
    """`(subcommand, arguments)` for a segment running `git worktree
    add`/`move`, or None. The command word is not read, so a wrapper —
    `env`, `sudo`, `xargs`, an absolute path to git — is still seen; but some
    token has to name git, or prose that only says `worktree add` is refused.
    """
    if not any(_names_git(token) for token in argv):
        return None
    for i, token in enumerate(argv):
        if token == "worktree" and argv[i + 1 : i + 2] in (["add"], ["move"]):
            return argv[i + 1], argv[i + 2 :]
    return None


def _takes_the_next_token(flag: str) -> bool:
    """Whether `flag`'s value is the token after it rather than inside it.

    A short cluster hands the rest of itself to the first value-taking option
    in it, so `-bx` and `-fbx` already carry the branch and only `-b` and `-fb`
    reach for the next token — a form git accepts.
    """
    if flag.startswith("--"):
        return flag in VALUE_LONG_FLAGS
    for end, char in enumerate(flag[1:], start=2):
        if char in VALUE_SHORT_FLAGS:
            return len(flag) == end
    return False


def _destination(subcommand: str, args: list[str]) -> str | None:
    """The path the segment would write to, `""` when it names none, or None
    when it writes nothing at all. `move` names the worktree first and the
    destination second."""
    wanted = 2 if subcommand == "move" else 1
    positional: list[str] = []
    rest = list(args)
    while rest:
        token = rest.pop(0)
        if token in HELP_FLAGS:
            return None
        if token == "--":
            positional.extend(rest)
            break
        if token.startswith("-"):
            if _takes_the_next_token(token) and rest:
                rest.pop(0)
            continue
        positional.append(token)
    return positional[wanted - 1] if len(positional) >= wanted else ""


def _in_a_checkouts_worktrees_dir(target: Path) -> bool:
    """Whether `target` sits under the `.claude/worktrees/` of a checkout —
    a directory carrying a `.git` — other than the home directory's own."""
    home = Path.home().resolve()
    for parent in target.parents:
        if parent.name == "worktrees" and parent.parent.name == ".claude":
            root = parent.parent.parent
            return root != home and (root / ".git").exists()
    return False


def _refusal(target: str, base: Path, cwd: str) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(target))
    resolved = (base / expanded).resolve() if target else None
    if resolved is not None and _in_a_checkouts_worktrees_dir(resolved):
        return None
    allowed = Path(os.environ.get("CLAUDE_PROJECT_DIR", cwd or ".")) / WORKTREE_DIR
    return DENY.format(allowed=allowed, target=resolved or "(none given)")


def _segment_verdict(command: _shell.Command, base: Path, cwd: str) -> str | None:
    for inner in _inner_commands(command):
        reason = verdict(inner, str(base))
        if reason:
            return reason
    argv = command.argv
    found = _worktree_args(argv)
    if found is None:
        return None
    target = _destination(*found)
    if target is None:
        return None
    return _refusal(target, base / os.path.expanduser(_chdir(argv)), cwd)


def verdict(cmd: str, cwd: str) -> str | None:
    """Why `cmd`, run in `cwd`, may not create the worktree it asks for — or
    None when it creates none outside `.claude/worktrees/`."""
    base = Path(cwd or ".")
    stack: list[Path] = []
    reading = _shell.read(cmd)
    for command in reading.commands:
        argv = _shell.strip_prefix(command.argv)
        if not argv:
            continue
        base, argv = _after_directory_change(argv, base, stack)
        if not argv:
            continue
        reason = _segment_verdict(command, base, cwd)
        if reason:
            return reason
    if reading.unreadable and _names_a_worktree_command(reading.unreadable):
        return UNREADABLE.format(text=reading.unreadable)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a parse failure
    reason = verdict(
        (payload.get("tool_input") or {}).get("command") or "",
        payload.get("cwd") or "",
    )
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
