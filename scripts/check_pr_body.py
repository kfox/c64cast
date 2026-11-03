#!/usr/bin/env python3
"""Refuse a pull-request description that strays from `.github/PULL_REQUEST_TEMPLATE.md`.

A PR description has one reader, the reviewer, and one job: let them judge
the change. Everything else that kept landing in descriptions -- a backlog of
what the change leaves undone, the findings a review declined, the machine a
hardware test ran on, a tool's attribution trailer -- outlives its use in the
record, and a backlog there is where follow-ups go unread.

The check closes the class rather than listing section names: a heading the
template does not declare is refused, so a new "Known residuals" fails the same
way "Follow-ups" does. The phrase and pattern lists below catch the same
content when it is written as prose instead of under a heading.

Two callers share this one implementation: the `pr-body` workflow, which reads
the body GitHub stored, and `.claude/hooks/check-pr-body.py`, which reads the
body an agent is about to send.

    check_pr_body.py FILE     # or `-` for stdin
"""

from __future__ import annotations

import ipaddress
import re
import sys
from dataclasses import dataclass

TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"

WHAT = "What changed and why"
LOOK = "What to look at"
VERIFIED = "How it was verified"
SECTIONS = (WHAT, LOOK, VERIFIED)
REQUIRED = (WHAT, VERIFIED)

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE = re.compile(r"^\s*(```|~~~)")
_ATX_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_LINE = re.compile(r"^\s*\*\*([^*]+?)\*\*:?\s*$")
_SETEXT_UNDERLINE = re.compile(r"^\s{0,3}(?:=+|-+)\s*$")
_NOT_A_PARAGRAPH = re.compile(r"^\s*(?:[-*+>|]|\d+[.)])(?:\s|$)")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")

_ATTRIBUTION = (
    re.compile(r"\bClaude\b"),
    re.compile(r"\bAnthropic\b"),
    re.compile(r"claude\.ai"),
    re.compile(r"(?i)\bco-authored-by\b"),
    re.compile(r"(?i)\bgenerated with \["),
    re.compile("\N{ROBOT FACE}"),
)

_MACHINE_SPECIFIC = (
    re.compile(r"(?<![\w.])/(?:Users|home)/[^/\s]+"),
    re.compile(r"(?i)\bcam ?link\b"),
)

_UNDONE_OR_REVIEW_NARRATIVE = (
    re.compile(r"(?i)\bfollow[- ]?ups?\b"),
    re.compile(r"(?i)\bnot in this (?:PR|change|branch)\b"),
    re.compile(r"(?i)\bout of scope\b"),
    re.compile(r"(?i)\bleft for later\b"),
    re.compile(r"(?i)\bknown residuals?\b"),
    re.compile(r"(?i)\bdeclined\b"),
    re.compile(r"(?i)\bpending (?:hardware|hw|re-?tests?|verification)\b"),
    re.compile(r"(?i)\breset (?:it |the (?:machine|device|U64) )?afterwards?\b"),
)


@dataclass(frozen=True)
class Line:
    number: int
    text: str
    in_code: bool


def _lines(body: str) -> list[Line]:
    """The body's lines with HTML comments blanked, so template hints never count.

    Line numbers survive the blanking because a comment's newlines are kept.
    """
    visible = _HTML_COMMENT.sub(lambda match: "\n" * match.group().count("\n"), body)

    lines = []
    in_code = False
    for number, text in enumerate(visible.splitlines(), start=1):
        if _FENCE.match(text):
            lines.append(Line(number, text, True))
            in_code = not in_code
            continue
        lines.append(Line(number, text, in_code))
    return lines


def _heading(line: Line) -> str | None:
    if line.in_code:
        return None
    match = _ATX_HEADING.match(line.text) or _BOLD_LINE.match(line.text)
    return match.group(1).strip().rstrip(":") if match else None


def _setext_heading(line: Line, following: Line | None) -> str | None:
    if following is None or following.in_code or line.in_code or not line.text.strip():
        return None
    if not _SETEXT_UNDERLINE.match(following.text):
        return None
    if _NOT_A_PARAGRAPH.match(line.text) or _SETEXT_UNDERLINE.match(line.text):
        return None
    return line.text.strip().rstrip(":")


def _private_addresses(text: str) -> list[str]:
    found = []
    for candidate in _IPV4.findall(text):
        try:
            address = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_unspecified:
            found.append(candidate)
    return found


def problems(body: str) -> list[str]:
    lines = _lines(body)
    found: list[str] = []

    section_text: dict[str, list[str]] = {}
    current: str | None = None
    underlines: set[int] = set()
    for index, line in enumerate(lines):
        if index in underlines:
            continue
        following = lines[index + 1] if index + 1 < len(lines) else None
        heading = _heading(line)
        if heading is None:
            heading = _setext_heading(line, following)
            if heading is not None:
                underlines.add(index + 1)
        if heading is None:
            if current is not None and line.text.strip():
                section_text[current].append(line.text)
            continue
        if heading not in SECTIONS:
            found.append(
                f"line {line.number}: heading {heading!r} is not one the template declares"
            )
            current = None
            continue
        if heading in section_text:
            found.append(f"line {line.number}: heading {heading!r} appears twice")
        current = heading
        section_text.setdefault(heading, [])

    for name in REQUIRED:
        if name not in section_text:
            found.append(f"missing the {name!r} section")
        elif not section_text[name]:
            found.append(f"the {name!r} section is empty")

    for line in lines:
        for pattern in (*_ATTRIBUTION, *_MACHINE_SPECIFIC, *_UNDONE_OR_REVIEW_NARRATIVE):
            match = pattern.search(line.text)
            if match:
                found.append(
                    f"line {line.number}: {match.group()!r} does not belong in a PR description"
                )
        for address in _private_addresses(line.text):
            found.append(f"line {line.number}: LAN address {address!r} is machine-specific")

    return found


def report(found: list[str]) -> str:
    listed = "\n".join(f"  - {item}" for item in found)
    return (
        f"PR description refused\n\n{listed}\n\n"
        f"Use the sections in {TEMPLATE}: {', '.join(repr(name) for name in SECTIONS)}.\n"
        f"Work this change leaves undone is fixed here or filed as an issue; review\n"
        f"history and machine details go to the conversation, not the description."
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_pr_body.py FILE|-", file=sys.stderr)
        return 2

    if argv[1] == "-":
        body = sys.stdin.read()
    else:
        with open(argv[1], encoding="utf-8") as handle:
            body = handle.read()

    found = problems(body)
    if not found:
        return 0

    print(report(found), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
