#!/usr/bin/env python3
"""Refuse an over-long commit message at write time, before review can spend a round on it.

Every sentence in a message is a claim someone reads as fact and cannot check
cheaply, so volume predicts how many are wrong. Length is the one part of that a
hook can decide, and deciding it here is free: the same observation raised during
review costs a round, and acting on it costs a reword, which moves that SHA and
every SHA stacked on it.

The defaults were measured rather than inherited: across the 200 commits merged
before this landed, the conventional 72-character subject would have refused 41
of them, while the bodies authored one commit at a time ran 4 to 12 non-blank
lines. Hence 80 and 10. Override:

    git config prose.subjectMax 100
    git config prose.bodyMax 20

Messages git composes itself -- merge, revert, and the `fixup!`/`squash!`/`amend!`
autosquash forms -- are skipped, since their shape is not the author's to choose.
Installed as a `commit-msg` hook, so `git commit --no-verify` is the deliberate
way past it.
"""

from __future__ import annotations

import itertools
import re
import subprocess
import sys

DEFAULT_SUBJECT_MAX = 80
DEFAULT_BODY_MAX = 10
DEFAULT_COMMENT_CHAR = "#"

_CONFIG_TIMEOUT_S = 10
_GENERATED_SUBJECT = re.compile(r"^(?:Merge\b|Revert\b|fixup!|squash!|amend!)")
_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
_SCISSORS_RULE = "------------------------ >8 ------------------------"


def _git_config(name: str) -> str:
    """`git config --get <name>`, or "" when it is unset or git cannot be asked."""
    try:
        done = subprocess.run(
            ["git", "config", "--get", name],
            capture_output=True,
            text=True,
            timeout=_CONFIG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    return done.stdout.strip()


def _configured(name: str, fallback: int) -> int:
    """`git config prose.<name>`, or `fallback` when it is unset or unusable."""
    try:
        value = int(_git_config(f"prose.{name}"))
    except ValueError:
        return fallback

    return value if value > 0 else fallback


def _comment_char() -> str:
    """`git config core.commentChar`, or `#` when unset, multi-character, or `auto`.

    Git resolves `auto` against the message it is about to write, which a hook
    holding only the finished file cannot redo, and it picks `#` unless a line of
    that message already starts with one.
    """
    char = _git_config("core.commentChar")

    return char if len(char) == 1 else DEFAULT_COMMENT_CHAR


def message_lines(raw: str, comment_char: str = DEFAULT_COMMENT_CHAR) -> list[str]:
    """The message as git will store it: comments and the scissors tail removed."""
    scissors = f"{comment_char} {_SCISSORS_RULE}"

    lines: list[str] = []
    for line in raw.splitlines():
        if line.rstrip() == scissors:
            break
        if line.startswith(comment_char):
            continue
        lines.append(line.rstrip())

    while lines and not lines[0].strip():
        lines.pop(0)

    while lines and not lines[-1].strip():
        lines.pop()

    return lines


def _paragraphs(lines: list[str]) -> list[list[str]]:
    """The blank-line-separated runs of non-blank lines."""
    return [
        list(run)
        for blank, run in itertools.groupby(lines, key=lambda line: not line.strip())
        if not blank
    ]


def body_lines(lines: list[str]) -> list[str]:
    """Non-blank body lines, less a trailing paragraph that holds only trailers.

    The block is the last paragraph and has to be preceded by one, the way git
    reads trailers. Popping trailer-shaped lines one at a time instead let a
    whole body of `Note:`-shaped prose count as nothing.
    """
    paragraphs = _paragraphs(lines[1:])

    if len(paragraphs) > 1 and all(_TRAILER.match(line) for line in paragraphs[-1]):
        paragraphs.pop()

    return [line for paragraph in paragraphs for line in paragraph]


def violations(lines: list[str], subject_max: int, body_max: int) -> list[str]:
    subject = lines[0]
    found = []

    if len(subject) > subject_max:
        found.append(
            f"subject is {len(subject)} characters, over the {subject_max} allowed:\n    {subject}"
        )

    if len(lines) > 1 and lines[1].strip():
        found.append("no blank line between the subject and the body")

    body = body_lines(lines)
    if len(body) > body_max:
        found.append(
            f"body is {len(body)} non-blank lines, over the {body_max} allowed "
            f"(trailers do not count)"
        )

    return found


def report(found: list[str], subject_max: int, body_max: int) -> None:
    print("commit message refused\n", file=sys.stderr)
    for item in found:
        print(f"  - {item}", file=sys.stderr)
    print(
        f"\nSay what the change does and the one or two facts that make it the\n"
        f"right change, then stop. Every extra sentence is a claim a reader takes\n"
        f"on faith and a reviewer can dispute.\n"
        f"\n"
        f"Caps are prose.subjectMax={subject_max} and prose.bodyMax={body_max};\n"
        f"raise them for this repository with `git config prose.bodyMax N`.",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_commit_message.py <message-file>", file=sys.stderr)
        return 0

    try:
        with open(argv[1], encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        print(f"could not read the commit message ({error}); not blocking", file=sys.stderr)
        return 0

    lines = message_lines(raw, _comment_char())
    if not lines or _GENERATED_SUBJECT.match(lines[0]):
        return 0

    subject_max = _configured("subjectMax", DEFAULT_SUBJECT_MAX)
    body_max = _configured("bodyMax", DEFAULT_BODY_MAX)

    found = violations(lines, subject_max, body_max)
    if not found:
        return 0

    report(found, subject_max, body_max)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
