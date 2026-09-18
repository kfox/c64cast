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
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

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
HEREDOC = "<<"
SUBSTITUTION = "`$()"
SEPARATORS = frozenset({"&", "&&", "(", ")", ";", ";;", "|", "||"})
REDIRECTS = frozenset({"<", "<&", "<<", "<<<", "<>", ">", ">&", ">>", ">|"})

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


def _tokens(text: str) -> list[str] | None:
    """`text` split the way a shell splits a command, with the separators,
    redirections and heredoc markers kept as tokens of their own — or None when
    a quote it opens is never closed, which a shell answers by reading on."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _line_segments(tokens: list[str]) -> tuple[list[list[str]], str]:
    """The commands in `tokens`, one argv each, with the heredoc delimiter they
    open.

    Adjacent punctuation arrives as one token — `;(` rather than `;` and `(` —
    which matches no separator and leaves the commands either side of it in one
    argv. That is read rather than split, because a segment is only a place to
    look for `git worktree add` and finding it there is the same answer; a
    split would instead carry a subshell's `cd` out to the commands after it.

    A redirection's file descriptor arrives as a token of its own, because
    shlex splits `2>` into `2` and `>`. It is dropped with the redirection, so
    it cannot sit in the argv and shift `move`'s second positional past the
    destination.
    """
    segments: list[list[str]] = [[]]
    heredoc = ""
    redirect = ""
    for token in tokens:
        if redirect:
            heredoc = token.lstrip("-") if redirect == HEREDOC else heredoc
            redirect = ""
        elif token in SEPARATORS:
            segments.append([])
        elif token in REDIRECTS:
            if segments[-1] and segments[-1][-1].isdigit():
                segments[-1].pop()
            redirect = token
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment], heredoc


def _segments(cmd: str) -> tuple[list[list[str]], str]:
    """One argv per command in `cmd`, and the tail of it that stayed unreadable.

    A heredoc body is skipped: its lines are text rather than commands, and a
    trailing backslash in one is text too — joining it to the next line would
    swallow the terminator and read the rest of the command as more body. A
    line that leaves a quote open is joined to the next instead, which is what
    a shell does with it.
    """
    segments: list[list[str]] = []
    delimiter = ""
    pending = ""
    for line in cmd.splitlines():
        if delimiter:
            delimiter = "" if line.strip() == delimiter else delimiter
            continue
        pending = f"{pending}\n{line}" if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        tokens = _tokens(pending)
        if tokens is None:
            continue
        pending = ""
        found, delimiter = _line_segments(tokens)
        segments.extend(found)
    return segments, pending


def _names_a_worktree_command(text: str) -> bool:
    """Whether unreadable text mentions the command this hook decides on. Read
    on text no lexer could take apart, so it matches words rather than tokens."""
    return "git" in text and "worktree" in text and ("add" in text or "move" in text)


def _strip_env(argv: list[str]) -> list[str]:
    """`argv` without its leading `VAR=value` assignments."""
    while argv and "=" in argv[0] and argv[0].split("=", 1)[0].isidentifier():
        argv = argv[1:]
    return argv


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


def _inner_commands(argv: list[str]) -> list[str]:
    """The command strings a segment hands to another shell, which have to be
    read as commands rather than as arguments. The shell is looked for
    anywhere in the segment, because a wrapper in front of it — `env`,
    `nohup`, `timeout` — keeps it out of `argv[0]`."""
    for i, token in enumerate(argv):
        name = Path(token).name
        if name == "eval":
            return [" ".join(argv[i + 1 :])]
        if name in SHELLS:
            return _shell_payload(argv[i + 1 :])
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


def _segment_verdict(argv: list[str], base: Path, cwd: str) -> str | None:
    for inner in _inner_commands(argv):
        reason = verdict(inner, str(base))
        if reason:
            return reason
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
    segments, unreadable = _segments(cmd)
    for segment in segments:
        argv = _strip_env(segment)
        if not argv:
            continue
        base, argv = _after_directory_change(argv, base, stack)
        if not argv:
            continue
        reason = _segment_verdict(argv, base, cwd)
        if reason:
            return reason
    if unreadable and _names_a_worktree_command(unreadable):
        return UNREADABLE.format(text=unreadable)
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
