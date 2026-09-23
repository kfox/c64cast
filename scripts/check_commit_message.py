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

import re
import subprocess
import sys

DEFAULT_SUBJECT_MAX = 80
DEFAULT_BODY_MAX = 10

_CONFIG_TIMEOUT_S = 10
_GENERATED_SUBJECT = re.compile(r"^(?:Merge\b|Revert\b|fixup!|squash!|amend!)")
_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
_SCISSORS = "# ------------------------ >8 ------------------------"


def _configured(name: str, fallback: int) -> int:
    """`git config prose.<name>`, or `fallback` when it is unset or unusable."""
    try:
        done = subprocess.run(
            ["git", "config", "--get", f"prose.{name}"],
            capture_output=True,
            text=True,
            timeout=_CONFIG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return fallback

    try:
        value = int(done.stdout.strip())
    except ValueError:
        return fallback

    return value if value > 0 else fallback


def message_lines(raw: str) -> list[str]:
    """The message as git will store it: comments and the scissors tail removed."""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.rstrip() == _SCISSORS:
            break
        if line.startswith("#"):
            continue
        lines.append(line.rstrip())

    while lines and not lines[0].strip():
        lines.pop(0)

    while lines and not lines[-1].strip():
        lines.pop()

    return lines


def body_lines(lines: list[str]) -> list[str]:
    """Non-blank body lines that are not part of the trailing trailer block."""
    body = [line for line in lines[1:] if line.strip()]

    while body and _TRAILER.match(body[-1]):
        body.pop()

    return body


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

    lines = message_lines(raw)
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
