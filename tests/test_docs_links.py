"""Repo-wide guards on what the documentation points at.

A dead link is invisible in review and invisible in CI unless something looks
for it, and the one that keeps coming back is a link to a document that was
folded into a book: the prose it replaced reads fine, so nobody notices the
destination is gone.
"""

from __future__ import annotations

import unittest
from pathlib import Path

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


def _text_files() -> list[Path]:
    found = []
    stack = [_REPO_ROOT]
    while stack:
        for entry in stack.pop().iterdir():
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.suffix in _TEXT_SUFFIXES or entry.name == "Makefile":
                found.append(entry)
    return found


class RetiredDocsTest(unittest.TestCase):
    def test_nothing_points_at_the_retired_usage_document(self) -> None:
        """`docs/usage.md` was promoted into the Programmer's Reference Guide.

        It was linked from fourteen places, so the failure mode this guards
        against is a reflex: a new cross-reference written the way every
        surrounding one used to be. Point it at `docs/reference/` instead.
        """
        needle = "usage.md"
        # Two files have to say the name: this one, which searches for it, and
        # the changelog, which records the removal and would be useless if it
        # could not name what was removed.
        allowed = {Path(__file__).resolve(), _REPO_ROOT / "CHANGELOG.md"}
        offenders = [
            str(path.relative_to(_REPO_ROOT))
            for path in _text_files()
            if path not in allowed and needle in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(offenders, [], "these still point at the retired usage document")


if __name__ == "__main__":
    unittest.main()
