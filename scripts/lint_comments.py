#!/usr/bin/env python3
"""Flag the mechanically decidable banned comment classes in the lines a commit adds.

The code is the narrative, and a comment restating it doubles the reading cost
and rots into a lie the moment the code moves. Three of the banned classes can
be decided without judgment, so they are decided here rather than in review:

- **section banners** -- a rule of punctuation, or `Step 3:` numbering
- **`TODO:` / `FIXME:` / `HACK:` markers** -- the issue tracker's job
- **commented-out code** -- version control already remembers it

Narration, self-justification and war stories stay judgment on purpose. Lexical
rules for them were measured against this tree's 13,704 standalone comments and
matched the legitimate categories -- an upstream quirk, the provenance of a
measured constant, a cross-file contract -- several times more often than the
narration they were aimed at. A hook that cries wolf is a hook people learn to
bypass. CLAUDE.md's "Code comments" section carries the full policy.

Only the comments a diff **adds** are examined, never the file around them: a
comment already in the tree is not this commit's business, and a hook that
reports one is a hook that punishes whoever touches the file next. That also
means an all-files run, and CI, have nothing staged to look at, which is
deliberate -- this is a write-time check, not a gate.

Turn it off for a repository with `git config prose.lintComments false`.

Known limit: a configuration sample pasted into a `#` comment (`enabled = true`)
parses as an assignment and is reported as commented-out code. Put samples in a
docstring, which this does not read.
"""

from __future__ import annotations

import ast
import io
import pathlib
import re
import subprocess
import sys
import tokenize

_DIFF_TIMEOUT_S = 60
_CONFIG_TIMEOUT_S = 10

_DIRECTIVE = re.compile(
    r"^#\s*(?:type:|noqa|pyright:|mypy:|pragma|fmt:|ruff:|nosec|isort:|coding[:=]|!|:schema)"
)
_URL = re.compile(r"https?://")
_BANNER = re.compile(r"^#\s*(?:[-=*#~_+]{3,}\s*)+$|^#\s*(?:Step|STEP|Part|PART)\s*\d+\b")
_MARKER = re.compile(r"\b(?:TODO|FIXME|HACK)\b\s*[:(]")

_DIFF_HEADER = "diff --git "
_FILE_HEADER = re.compile(r"^\+\+\+ (?:b/)?(.*?)\t?$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_STATEMENT_NODES = (
    ast.Assign,
    ast.AugAssign,
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Return,
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.Try,
    ast.Raise,
    ast.Assert,
    ast.Delete,
)
_MIN_CODE_LENGTH = 4


def _disabled() -> bool:
    try:
        done = subprocess.run(
            ["git", "config", "--get", "prose.lintComments"],
            capture_output=True,
            text=True,
            timeout=_CONFIG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return done.stdout.strip().lower() in {"false", "0", "no", "off"}


def _parses_as_statements(text: str) -> bool:
    try:
        parsed = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False

    return bool(parsed.body) and all(isinstance(node, _STATEMENT_NODES) for node in parsed.body)


def looks_like_code(body: str) -> bool:
    """Whether a comment's text parses as Python statements that do something.

    Annotations are excluded because `Label: some prose` parses as one, which is
    how most comments in a real tree open. A block header (`for x in xs:`) is
    incomplete on its own, so it gets a `pass` body before the second attempt --
    commenting out a block is the common way commented-out code arrives.
    """
    text = body.strip()
    if len(text) < _MIN_CODE_LENGTH:
        return False

    if _parses_as_statements(text):
        return True

    return text.endswith(":") and _parses_as_statements(f"{text}\n    pass")


def classify(line: str) -> str | None:
    """The banned class a standalone comment line falls into, if any."""
    text = line.strip()
    if not text.startswith("#"):
        return None
    if _DIRECTIVE.match(text) or _URL.search(text):
        return None

    if _BANNER.match(text):
        return "section banner"
    if _MARKER.search(text):
        return "TODO/FIXME marker"
    if looks_like_code(text.lstrip("#").lstrip(":")):
        return "commented-out code"

    return None


def added_lines(paths: list[str]) -> list[tuple[str, int, str]]:
    """Every line the staged diff adds, as (path, line number, text)."""
    try:
        done = subprocess.run(
            # Without core.quotePath off, git C-escapes a non-ASCII path and
            # wraps the whole of it in double quotes, inside the `b/` prefix.
            ["git", "-c", "core.quotePath=false"]
            + ["diff", "--cached", "--no-color", "-U0", "--", *paths],
            capture_output=True,
            text=True,
            timeout=_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if done.returncode != 0:
        return []

    added: list[tuple[str, int, str]] = []
    path = ""
    number = 0
    in_hunk = False

    for line in done.stdout.splitlines():
        if line.startswith(_DIFF_HEADER):
            path, in_hunk = "", False
            continue

        # Inside a hunk, `+++` is an added line whose own text starts with `++`.
        if not in_hunk:
            header = _FILE_HEADER.match(line)
            if header:
                path = header[1]
                continue

        hunk = _HUNK_HEADER.match(line)
        if hunk:
            number = int(hunk[1])
            in_hunk = True
            continue

        if in_hunk and line.startswith("+"):
            if path and path != "/dev/null":
                added.append((path, number, line[1:]))
            number += 1

    return added


def comment_lines(path: str) -> set[int] | None:
    """Which lines of the staged `path` hold a real comment, or None if unknown.

    A line-oriented walk cannot tell a comment from a `#`-leading line inside a
    string, so a shell or TOML sample in a docstring would be reported as one.
    None means the staged blob did not tokenize, and the caller keeps its
    line-oriented verdict rather than dropping it.
    """
    try:
        done = subprocess.run(
            ["git", "show", f":{path}"],
            capture_output=True,
            text=True,
            timeout=_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if done.returncode != 0:
        return None

    try:
        tokens = tokenize.generate_tokens(io.StringIO(done.stdout).readline)
        return {token.start[0] for token in tokens if token.type == tokenize.COMMENT}
    except (SyntaxError, UnicodeDecodeError, ValueError, tokenize.TokenError):
        return None


def findings(paths: list[str]) -> list[tuple[str, int, str, str]]:
    found = []
    comments: dict[str, set[int] | None] = {}

    for path, number, text in added_lines(paths):
        banned = classify(text)
        if banned is None:
            continue

        if path not in comments:
            comments[path] = comment_lines(path)

        real = comments[path]
        if real is not None and number not in real:
            continue

        found.append((path, number, banned, text.strip()))

    return found


def report(found: list[tuple[str, int, str, str]]) -> None:
    print("comments this commit adds that the guidelines exclude\n", file=sys.stderr)
    for path, number, banned, text in found:
        print(f"  {path}:{number}: {banned}", file=sys.stderr)
        print(f"      {text}", file=sys.stderr)

    print(
        "\nDelete them. A banner is a sign the file wants splitting, a TODO belongs\n"
        "in the tracker, and version control already remembers commented-out code.\n"
        "\n"
        "Disable for this repository with `git config prose.lintComments false`,\n"
        "or bypass once with `git commit --no-verify`.",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    paths = [p for p in argv[1:] if pathlib.Path(p).suffix in {".py", ".pyi"}]
    if not paths or _disabled():
        return 0

    found = findings(paths)
    if not found:
        return 0

    report(found)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
