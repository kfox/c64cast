#!/usr/bin/env python3
"""Cut a release: stamp the version into `pyproject.toml` and the changelog.

    python scripts/bump_version.py 0.2.0          # cut 0.2.0, dated today
    python scripts/bump_version.py 0.2.0 --date 2026-08-01
    python scripts/bump_version.py --check 0.2.0  # verify, change nothing
    python scripts/bump_version.py --notes 0.2.0  # print that section's body

Cutting collects every fragment under `changelog.d/` into the new version's
section and deletes the files; `changelog.d/README.md` has the fragment format.
`--check` and `--notes` write nothing. See RELEASING.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
FRAGMENT_DIR = REPO_ROOT / "changelog.d"
UV_LOCK = REPO_ROOT / "uv.lock"

REPO_URL = "https://github.com/kfox/c64cast"

# Local versions and epochs are excluded: neither belongs on a tag.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.dev\d+)?$")

PYPROJECT_VERSION_RE = re.compile(r'^version = "([^"]+)"$', re.M)

UNRELEASED_HEADING = "## [Unreleased]"

UNRELEASED_POINTER = (
    "Entries for the next release are one file each under `changelog.d/` (its\n"
    "README.md has the format), and are collected here when the release is cut."
)

# Release-body order: Upgrade notes lead, the rest follow Keep a Changelog.
FRAGMENT_CATEGORIES = {
    "upgrade-notes": "Upgrade notes",
    "added": "Added",
    "changed": "Changed",
    "deprecated": "Deprecated",
    "removed": "Removed",
    "fixed": "Fixed",
    "security": "Security",
}

FRAGMENT_README = "README.md"
FRAGMENT_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A heading line inside an entry would split it out of its bullet list and
# break the Upgrade-notes-first rule; fenced blocks may hold `#` comments.
FRAGMENT_HEADING_RE = re.compile(r"^#{1,6}[ \t]")

# `[ \t]*` rather than `\s*`, which would span the newline these offsets splice at.
SECTION_RE = re.compile(r"^## \[([^\]]+)\](?:[ \t]+-[ \t]+(\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M)

LINK_REF_RE = re.compile(r"^\[([^\]]+)\]: (\S+)$", re.M)


class BumpError(Exception):
    """A problem that must stop the release. Message is operator-readable."""


def pyproject_version(text: str | None = None) -> str:
    """The version currently declared in `pyproject.toml`."""
    if text is None:
        text = PYPROJECT.read_text(encoding="utf-8")
    version = tomllib.loads(text).get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise BumpError("pyproject.toml has no [project] version")
    return version


def lock_version(text: str | None = None) -> str:
    """The version `uv.lock` records for c64cast itself."""
    if text is None:
        text = UV_LOCK.read_text(encoding="utf-8")
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
    """The prose under `## [version]`, up to the next heading or the link refs."""
    headings = list(SECTION_RE.finditer(changelog))
    for i, match in enumerate(headings):
        if match.group(1) != version:
            continue
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(changelog)
        # Link refs belong to the document, not to whichever section they follow.
        body = LINK_REF_RE.sub("", changelog[start:end])
        return body.strip() + "\n"
    raise BumpError(f"CHANGELOG.md has no '## [{version}]' section")


class Fragment(NamedTuple):
    name: str
    category: str
    text: str


def _fragment_problem(name: str, text: str) -> str | None:
    """Why `changelog.d/<name>` is not a valid fragment, or None if it is."""
    if not name.endswith(".md"):
        return f"changelog.d/{name}: fragments are named <slug>.<category>.md"
    slug, _, category = name[: -len(".md")].rpartition(".")
    if not FRAGMENT_SLUG_RE.match(slug):
        return (
            f"changelog.d/{name}: slug {slug!r} must be lowercase letters, digits and "
            "single hyphens, as in <slug>.<category>.md"
        )
    if category not in FRAGMENT_CATEGORIES:
        return (
            f"changelog.d/{name}: category {category!r} is not one of "
            f"{', '.join(FRAGMENT_CATEGORIES)}"
        )
    if not text.strip():
        return f"changelog.d/{name}: the fragment is empty"
    if not text.startswith("- "):
        return f"changelog.d/{name}: an entry is a bullet and must start with '- '"
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and FRAGMENT_HEADING_RE.match(line):
            return f"changelog.d/{name}: a heading line ({line!r}) cannot appear inside an entry"
    if fenced:
        return f"changelog.d/{name}: a code fence is never closed"
    return None


def fragment_paths(directory: Path | None = None) -> list[Path]:
    """Everything in the fragment directory except its README, by slug."""
    root = FRAGMENT_DIR if directory is None else directory
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.iterdir() if path.name != FRAGMENT_README),
        key=lambda path: (path.name.removesuffix(".md").rpartition(".")[0], path.name),
    )


def fragment_problems(directory: Path | None = None) -> list[str]:
    """Every malformed entry in the fragment directory."""
    root = FRAGMENT_DIR if directory is None else directory
    if not root.is_dir():
        return [f"{root.name}/ is missing"]
    problems = []
    for path in fragment_paths(root):
        if not path.is_file():
            problems.append(f"{root.name}/{path.name}: not a file")
            continue
        problem = _fragment_problem(path.name, path.read_text(encoding="utf-8"))
        if problem is not None:
            problems.append(problem)
    return problems


def read_fragments(directory: Path | None = None) -> list[Fragment]:
    """Every fragment, ordered by filename. Raises BumpError on a malformed one."""
    root = FRAGMENT_DIR if directory is None else directory
    problems = fragment_problems(root)
    if problems:
        raise BumpError("malformed changelog fragments:\n  " + "\n  ".join(problems))
    return [
        Fragment(path.name, path.name[: -len(".md")].rpartition(".")[2], path.read_text("utf-8"))
        for path in fragment_paths(root)
    ]


def render_fragments(fragments: list[Fragment]) -> str:
    """The body of a version's section: one `###` subsection per category that
    has entries, in `FRAGMENT_CATEGORIES` order."""
    blocks = []
    for category, title in FRAGMENT_CATEGORIES.items():
        entries = [f.text.rstrip() for f in fragments if f.category == category]
        if entries:
            blocks.append(f"### {title}\n\n" + "\n\n".join(entries))
    return "\n\n".join(blocks) + "\n"


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
    """The link a changelog version heading points at."""
    if previous is None:
        return f"{REPO_URL}/releases/tag/v{version}"
    return f"{REPO_URL}/compare/v{previous}...v{version}"


def apply_changelog(text: str, version: str, date: str, body: str) -> str:
    """Insert a dated `version` section holding `body` after the Unreleased pointer."""
    # A whole line, not a substring: the preamble names the heading inline too.
    heading = re.search(rf"^{re.escape(UNRELEASED_HEADING)}[ \t]*$", text, re.M)
    if heading is None:
        raise BumpError(f"CHANGELOG.md has no '{UNRELEASED_HEADING}' section to cut")
    existing = released_versions(text)
    if version in existing:
        raise BumpError(f"CHANGELOG.md already has a '## [{version}]' section")
    if not body.strip():
        raise BumpError("changelog.d/ holds no fragments -- there is nothing to release")
    previous = existing[0] if existing else None

    following = SECTION_RE.search(text, heading.end())
    refs = LINK_REF_RE.search(text, heading.end())
    ends = [m.start() for m in (following, refs) if m is not None]
    insert_at = min(ends) if ends else len(text)
    text = (
        text[:insert_at].rstrip("\n")
        + f"\n\n## [{version}] - {date}\n\n{body.strip()}\n\n"
        + text[insert_at:]
    )

    old_unreleased = f"[Unreleased]: {REPO_URL}/commits/main"
    new_refs = (
        f"[Unreleased]: {REPO_URL}/compare/v{version}...HEAD\n"
        f"[{version}]: {compare_url(previous, version)}"
    )
    if old_unreleased in text:
        text = text.replace(old_unreleased, new_refs, 1)
    else:
        unreleased_ref = re.search(rf"^\[Unreleased\]: {re.escape(REPO_URL)}\S*$", text, re.M)
        if unreleased_ref is None:
            raise BumpError("CHANGELOG.md has no [Unreleased] link reference to update")
        text = text[: unreleased_ref.start()] + new_refs + text[unreleased_ref.end() :]
    return text


def relock() -> None:
    """Refresh `uv.lock` so its c64cast entry matches the new version.

    Without `--upgrade`, so a release does not also move dependency versions.
    """
    try:
        subprocess.run(["uv", "lock"], cwd=REPO_ROOT, check=True)
    except FileNotFoundError:
        raise BumpError("uv is not on PATH; re-run with --no-lock and `uv lock` by hand") from None
    except subprocess.CalledProcessError as exc:
        raise BumpError(f"`uv lock` failed (exit {exc.returncode})") from None


def check(version: str) -> list[str]:
    """Problems that would make releasing `version` from this tree wrong."""
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

    problems.extend(fragment_problems())
    leftover = [path.name for path in fragment_paths()]
    if leftover:
        problems.append(
            f"changelog.d/ still holds {len(leftover)} fragment(s) ({', '.join(leftover[:3])}"
            f"{', ...' if len(leftover) > 3 else ''}) -- a cut collects and deletes them, "
            "so an entry left behind would never reach the release notes"
        )
    return problems


def bump(version: str, date: str, do_lock: bool) -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")

    new_pyproject = apply_pyproject(pyproject, version)
    fragments = read_fragments()
    new_changelog = apply_changelog(changelog, version, date, render_fragments(fragments))

    PYPROJECT.write_text(new_pyproject, encoding="utf-8")
    CHANGELOG.write_text(new_changelog, encoding="utf-8")
    for fragment in fragments:
        (FRAGMENT_DIR / fragment.name).unlink()
    print(f"pyproject.toml  version -> {version}")
    print(f"CHANGELOG.md    {len(fragments)} fragment(s) -> [{version}] - {date}")

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
