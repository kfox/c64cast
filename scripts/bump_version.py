#!/usr/bin/env python3
"""Cut a release: stamp the version into `pyproject.toml` and the changelog.

The version lives in exactly one place -- `[project] version` in
`pyproject.toml` -- and everything else derives from it: `__version__` via
`importlib.metadata`, the User's Guide cover, the `#:schema` URL a generated
config points at, and the git tag the release workflow verifies. This script
is what moves that one number, plus the changelog bookkeeping that has to move
with it.

    python scripts/bump_version.py 0.2.0          # cut 0.2.0, dated today
    python scripts/bump_version.py 0.2.0 --date 2026-08-01
    python scripts/bump_version.py --check 0.2.0  # verify, change nothing
    python scripts/bump_version.py --notes 0.2.0  # print that section's body

`--check` is the release gate: it asserts that `pyproject.toml`, `uv.lock` and
the changelog all agree on the version being released, and that the changelog
section carries a real date rather than still saying "Unreleased". The release
workflow runs it against the pushed tag, so a tag that disagrees with the tree
fails before anything is published -- a wrong version on PyPI cannot be
replaced, only yanked.

`--notes` prints one section's body, which is what the workflow feeds to
`gh release create --notes-file`. It lives here because this is already the
module that knows the changelog's shape; a second parser in YAML would be one
more thing to keep in step with it.

Neither `--check` nor `--notes` writes anything, so both are safe to run
against a dirty tree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
UV_LOCK = REPO_ROOT / "uv.lock"

REPO_URL = "https://github.com/kfox/c64cast"

# PEP 440 releases and pre-releases, which is everything this project intends
# to publish. Local versions (`+local`) and epochs are deliberately not
# accepted: neither belongs on a tag.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.dev\d+)?$")

# The `[project]` version line. `^version = ` at line start matches only that
# one key in this file -- the other version-ish keys are all spelled
# differently (`requires-python`, `target-version`, `python_version`,
# `pythonVersion`) -- and `apply_pyproject` asserts the match count, so a
# future key that does collide fails loudly instead of being rewritten.
PYPROJECT_VERSION_RE = re.compile(r'^version = "([^"]+)"$', re.M)

UNRELEASED_HEADING = "## [Unreleased]"

# A changelog section heading: `## [Unreleased]` or `## [1.2.3] - 2026-07-29`.
# Trailing whitespace is `[ \t]*`, not `\s*`: `\s` matches newlines, so `\s*$`
# consumes the blank line after the heading, and a rewrite spliced at that
# offset glues the new heading to its own first paragraph.
SECTION_RE = re.compile(r"^## \[([^\]]+)\](?:[ \t]+-[ \t]+(\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M)

# A link-reference definition at the foot of the changelog.
LINK_REF_RE = re.compile(r"^\[([^\]]+)\]: (\S+)$", re.M)


class BumpError(Exception):
    """A problem that must stop the release. Message is operator-readable."""


# ---------------------------------------------------------------------------
# Reading current state
# ---------------------------------------------------------------------------


def pyproject_version(text: str | None = None) -> str:
    """The version currently declared in `pyproject.toml`."""
    if text is None:
        text = PYPROJECT.read_text(encoding="utf-8")
    version = tomllib.loads(text).get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise BumpError("pyproject.toml has no [project] version")
    return version


def lock_version(text: str | None = None) -> str:
    """The version `uv.lock` records for c64cast itself.

    The lockfile pins the project as one of its own entries, so it goes stale
    the moment the version moves. A `uv sync --frozen` (which is what CI runs)
    then fails with "lockfile is out of date" -- a confusing way to discover a
    half-finished release, hence the explicit check.
    """
    if text is None:
        text = UV_LOCK.read_text(encoding="utf-8")
    # The `[[package]]` entry whose name is ours; `version` is the next line.
    match = re.search(r'^name = "c64cast"\nversion = "([^"]+)"$', text, re.M)
    if match is None:
        raise BumpError("uv.lock has no c64cast package entry with a version")
    return match.group(1)


def sections(changelog: str) -> list[tuple[str, str | None]]:
    """Every `## [...]` heading in the changelog, in file order, as
    (name, date) -- date is None for the Unreleased section."""
    return [(m.group(1), m.group(2)) for m in SECTION_RE.finditer(changelog)]


def released_versions(changelog: str) -> list[str]:
    """Named versions in the changelog, newest first, excluding Unreleased."""
    return [name for name, _ in sections(changelog) if name != "Unreleased"]


def section_body(changelog: str, version: str) -> str:
    """The prose under `## [version]`, up to the next heading or the link refs.

    Used for the GitHub release notes, so it is the section's own text with the
    heading removed -- the release page already shows the version and date in
    its own title.
    """
    headings = list(SECTION_RE.finditer(changelog))
    for i, match in enumerate(headings):
        if match.group(1) != version:
            continue
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(changelog)
        body = changelog[start:end]
        # Trailing link-reference definitions belong to the whole document, not
        # to the last section, so strip any that fell inside this slice.
        body = LINK_REF_RE.sub("", body)
        return body.strip() + "\n"
    raise BumpError(f"CHANGELOG.md has no '## [{version}]' section")


# ---------------------------------------------------------------------------
# Rewrites
# ---------------------------------------------------------------------------


def apply_pyproject(text: str, version: str) -> str:
    """Return `text` with `[project] version` set to `version`."""
    matches = PYPROJECT_VERSION_RE.findall(text)
    if len(matches) != 1:
        raise BumpError(
            f'expected exactly one top-level `version = "..."` line in '
            f"pyproject.toml, found {len(matches)} -- the regex needs narrowing"
        )
    return PYPROJECT_VERSION_RE.sub(f'version = "{version}"', text, count=1)


def compare_url(previous: str | None, version: str) -> str:
    """The link a changelog version heading points at.

    A first release has nothing to compare against, so it links to its own tag;
    every release after that links to the diff from the one before, which is
    the form that actually gets clicked.
    """
    if previous is None:
        return f"{REPO_URL}/releases/tag/v{version}"
    return f"{REPO_URL}/compare/v{previous}...v{version}"


def apply_changelog(text: str, version: str, date: str) -> str:
    """Rename the Unreleased section to `version`, dated, and open a fresh one.

    Keep a Changelog's shape: a permanently-present Unreleased section that
    work accumulates under, and one dated section per release below it. Both
    the heading and the link reference at the foot move together.
    """
    # Matched as a whole line, not as a substring: the changelog's own preamble
    # explains the release process and mentions `## [Unreleased]` inline in
    # backticks, several paragraphs above the real heading. A plain
    # `str.replace(..., 1)` rewrites that sentence and leaves the heading alone,
    # which produces a file with no release section and no error.
    heading = re.search(rf"^{re.escape(UNRELEASED_HEADING)}[ \t]*$", text, re.M)
    if heading is None:
        raise BumpError(
            f"CHANGELOG.md has no '{UNRELEASED_HEADING}' section to cut -- "
            "was this version already released?"
        )
    existing = released_versions(text)
    if version in existing:
        raise BumpError(f"CHANGELOG.md already has a '## [{version}]' section")
    previous = existing[0] if existing else None

    # The Unreleased section becomes the release; a new empty one takes its
    # place. "Nothing yet." rather than a bare heading so the file reads as
    # deliberate between releases instead of looking truncated.
    text = (
        text[: heading.start()]
        + f"{UNRELEASED_HEADING}\n\nNothing yet.\n\n## [{version}] - {date}"
        + text[heading.end() :]
    )

    # Link refs: Unreleased now compares against the new tag, and the new
    # version gets its own ref directly above the previous newest.
    old_unreleased = f"[Unreleased]: {REPO_URL}/commits/main"
    new_refs = (
        f"[Unreleased]: {REPO_URL}/compare/v{version}...HEAD\n"
        f"[{version}]: {compare_url(previous, version)}"
    )
    if old_unreleased in text:
        # First release: the Unreleased ref still points at the branch.
        text = text.replace(old_unreleased, new_refs, 1)
    else:
        unreleased_ref = re.search(rf"^\[Unreleased\]: {re.escape(REPO_URL)}\S*$", text, re.M)
        if unreleased_ref is None:
            raise BumpError("CHANGELOG.md has no [Unreleased] link reference to update")
        text = text[: unreleased_ref.start()] + new_refs + text[unreleased_ref.end() :]
    return text


def relock() -> None:
    """Refresh `uv.lock` so its c64cast entry matches the new version.

    `uv lock` without `--upgrade` only resolves what it must, so this moves the
    project's own version and nothing else -- a release is not the moment to
    pull in new dependency versions.
    """
    try:
        subprocess.run(["uv", "lock"], cwd=REPO_ROOT, check=True)
    except FileNotFoundError:
        raise BumpError("uv is not on PATH; re-run with --no-lock and `uv lock` by hand") from None
    except subprocess.CalledProcessError as exc:
        raise BumpError(f"`uv lock` failed (exit {exc.returncode})") from None


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def check(version: str) -> list[str]:
    """Problems that would make releasing `version` from this tree wrong.

    Empty list means the tree is consistent and ready to tag.
    """
    problems: list[str] = []

    declared = pyproject_version()
    if declared != version:
        problems.append(f"pyproject.toml declares {declared}, expected {version}")

    locked = lock_version()
    if locked != version:
        problems.append(f"uv.lock records c64cast {locked}, expected {version} -- run `uv lock`")

    changelog = CHANGELOG.read_text(encoding="utf-8")
    dates = dict(sections(changelog))
    if version not in dates:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif dates[version] is None:
        problems.append(f"CHANGELOG.md's '## [{version}]' section is missing its date")
    if f"[{version}]: " not in changelog:
        problems.append(f"CHANGELOG.md has no '[{version}]:' link reference")

    return problems


def bump(version: str, date: str, do_lock: bool) -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")

    # Compute both rewrites before writing either: a failure halfway through
    # leaves a tree that is neither the old version nor the new one.
    new_pyproject = apply_pyproject(pyproject, version)
    new_changelog = apply_changelog(changelog, version, date)

    PYPROJECT.write_text(new_pyproject, encoding="utf-8")
    CHANGELOG.write_text(new_changelog, encoding="utf-8")
    print(f"pyproject.toml  version -> {version}")
    print(f"CHANGELOG.md    [Unreleased] -> [{version}] - {date}")

    if do_lock:
        relock()
        print(f"uv.lock         c64cast -> {lock_version()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("version", help="the version to release, e.g. 0.2.0 (no leading v)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify the tree is consistent for this version; write nothing",
    )
    mode.add_argument(
        "--notes",
        action="store_true",
        help="print this version's changelog section to stdout; write nothing",
    )
    ap.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="release date for the changelog heading (default: today)",
    )
    ap.add_argument(
        "--no-lock",
        action="store_true",
        help="skip `uv lock` (you must run it yourself before tagging)",
    )
    args = ap.parse_args(argv)

    version = args.version.removeprefix("v")
    if not VERSION_RE.match(version):
        print(
            f"error: {version!r} is not a release version (expected X.Y.Z, "
            "optionally with a1/b1/rc1/.dev1)",
            file=sys.stderr,
        )
        return 2

    try:
        if args.notes:
            print(section_body(CHANGELOG.read_text(encoding="utf-8"), version), end="")
            return 0
        if args.check:
            problems = check(version)
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            if problems:
                return 1
            print(f"tree is consistent for v{version}")
            return 0
        bump(version, args.date, do_lock=not args.no_lock)
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("\nnext: review the diff, commit on a release branch, open a PR.")
    print(f"once it is merged, tag main with v{version} to publish (see RELEASING.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
