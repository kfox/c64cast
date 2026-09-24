"""Repo-wide guards on what the documentation points at.

A dead link is invisible in review and invisible in CI unless something looks
for it, and the one that keeps coming back is a link to a document that was
folded into a book: the prose it replaced reads fine, so nobody notices the
destination is gone.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _child_process import run_bounded

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories with nothing to check and a great deal to read: build output,
# virtual environments, and the media tree, where a stray .md in a downloaded
# SID collection is not ours to police.
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "_site",  # `make site` output: generated HTML, not prose anyone edits
    "assets",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
}

_TEXT_SUFFIXES = {".cfg", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}


def _nested_checkout(path: Path) -> bool:
    """Whether `path` is a checkout of its own — worktree, clone or submodule —
    whose prose belongs to whatever it has checked out rather than to this tree.
    """
    return (path / ".git").exists()


def _text_files() -> list[Path]:
    found = []
    stack = [_REPO_ROOT]
    while stack:
        for entry in stack.pop().iterdir():
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not _nested_checkout(entry):
                    stack.append(entry)
            elif entry.suffix in _TEXT_SUFFIXES or entry.name == "Makefile":
                found.append(entry)
    return found


# A target is only checked when it looks like a path into the repo: it carries a
# separator, or an extension we ship. Without this, `[Ctx](ctx)` and
# `REGISTRY[name](seed=seed)` read as links.
_PATH_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".png",
    ".prg",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".typ",
    ".txt",
    ".yml",
}

# Link text may wrap across lines; the prose here is hard-wrapped at 80 columns,
# so a per-line pattern would skip the links it splits.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Prose that quotes the *shape* of a link rather than making one: the guide's
# authoring instructions, the renderers' docstrings describing what they accept,
# and the book builder's own fixtures.
_LINK_SHAPE_EXEMPT = {
    "CHANGELOG.md",  # a record; it quotes links as they were when removed
    "docs/guide/README.md",
    "scripts/bookdoc.py",
    "scripts/build_book.py",
    "scripts/build_site.py",
    "scripts/make_guide_figures.py",
    "scripts/make_reference_diagrams.py",
    "tests/test_book_build.py",
}


def _linked_paths(text: str) -> list[tuple[int, str]]:
    found = []
    for match in _MARKDOWN_LINK.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if any(ch in target for ch in "${}"):  # a shell or f-string template
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if "/" not in path and Path(path).suffix not in _PATH_SUFFIXES:
            continue
        found.append((text.count("\n", 0, match.start()) + 1, path))
    return found


def _linkable_files() -> list[Path]:
    """Every tracked Markdown file and every tracked Python file.

    `git ls-files` rather than a walk: it reaches `assets/`, which `_SKIP_DIRS`
    excludes, while still ignoring build output and anything untracked.
    """
    listing = run_bounded(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = []
    for name in listing.split("\0"):
        if not name:
            continue
        path = _REPO_ROOT / name
        if name.startswith("c64cast/web/dist/"):
            continue
        if path.suffix in {".md", ".py"}:
            files.append(path)
    return files


class RelativeLinkTest(unittest.TestCase):
    """Every relative link in prose and in Python source resolves to a file.

    Not redundant with its neighbors: `test_architecture_index` resolves the
    module table's rows but not the links inside the sections those rows point
    at, and the site build skips `docs/architecture/` entirely.
    """

    def test_every_relative_link_resolves(self) -> None:
        offenders = []
        for path in _linkable_files():
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _LINK_SHAPE_EXEMPT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, target in _linked_paths(text):
                if not (path.parent / target).resolve().exists():
                    offenders.append(f"{rel}:{lineno} -> {target}")
        self.assertEqual(offenders, [], "these links resolve to nothing")


class TextFileWalkTest(unittest.TestCase):
    def test_a_checkout_under_the_root_is_not_walked(self) -> None:
        """A second checkout of this repo below its own root would otherwise be
        read as this checkout's own prose, and fail these guards for whatever
        some other branch says. This repo's agent tooling adds worktrees under
        `.claude/worktrees/`, but the prune is by marker rather than by path:
        a linked worktree marks itself with a `.git` file and a clone with a
        `.git` directory, so both shapes are built here.
        """
        root = Path(tempfile.mkdtemp())
        (root / "ours.md").write_text("ours", encoding="utf-8")

        worktree = root / ".claude" / "worktrees" / "wt"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        (worktree / "theirs.md").write_text("theirs", encoding="utf-8")

        clone = root / "vendor" / "clone"
        (clone / ".git").mkdir(parents=True)
        (clone / "vendored.md").write_text("vendored", encoding="utf-8")

        with mock.patch(f"{__name__}._REPO_ROOT", root):
            found = {p.name for p in _text_files()}

        self.assertEqual(found, {"ours.md"})


class RetiredDocsTest(unittest.TestCase):
    def test_nothing_points_at_the_retired_usage_document(self) -> None:
        """`docs/usage.md` was promoted into the Programmer's Reference Guide.

        It was linked from fourteen places, so the failure mode this guards
        against is a reflex: a new cross-reference written the way every
        surrounding one used to be. Point it at `docs/reference/` instead.
        """
        needle = "usage.md"
        # Two files have to say the name: this one, and the changelog, which records
        # the removal and would be useless if it could not name what was removed.
        allowed = {Path(__file__).resolve(), _REPO_ROOT / "CHANGELOG.md"}
        offenders = [
            str(path.relative_to(_REPO_ROOT))
            for path in _text_files()
            if path not in allowed and needle in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(offenders, [], "these still point at the retired usage document")


if __name__ == "__main__":
    unittest.main()
