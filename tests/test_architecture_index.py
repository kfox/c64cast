"""Guards on the architecture reference's module index.

The index is the only route from a module to its notes, and both halves of it
rot silently: a new module is written and never listed, a section is renamed and
the row still points at the old anchor, a module is deleted and its row outlives
it. None of that fails anything -- `docs/architecture*` is deliberately not
published to the site, so the book/site renderers never read it, and a dead
in-repo anchor renders as a link that simply lands at the top of the page.

So this test asserts the two properties a reader relies on:

  * **Partition** -- every module in the tree appears exactly once, either in
    the index table or in the "Not covered here" list, and nothing appears that
    isn't a module.
  * **Resolution** -- every link in the index table points at a file that
    exists and an anchor that file actually has.

Anchors are slugged with the same `scripts/bookdoc.heading_slug` the PDF and the
site use, which is GitHub's own rule -- so a row that passes here resolves on
github.com too.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INDEX = _REPO_ROOT / "docs" / "architecture.md"
_PACKAGE = _REPO_ROOT / "c64cast"

# Registry subpackages the index lists whole, as directories: their files are
# small members of one registry apiece, and a per-file row would say the same
# thing forty times. Paths are relative to the package root.
_REGISTRY_DIRS = ("scenes/generators/", "video/modes/", "orchestrators/", "scenes/overlays/")


def _load_bookdoc():
    """scripts/ is not a package, so load it by path (as test_book_build does)."""
    path = _REPO_ROOT / "scripts" / "bookdoc.py"
    spec = importlib.util.spec_from_file_location("bookdoc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bookdoc"] = module
    spec.loader.exec_module(module)
    return module


_bookdoc = _load_bookdoc()


def _modules() -> set[str]:
    names = set()
    for path in _PACKAGE.rglob("*.py"):
        rel = path.relative_to(_PACKAGE).as_posix()
        if path.name == "__init__.py" or "__pycache__" in rel:
            continue
        if any(rel.startswith(prefix) for prefix in _REGISTRY_DIRS):
            continue
        names.add(rel)
    return names | set(_REGISTRY_DIRS)


def _table_rows(text: str, heading: str) -> list[tuple[str, str]]:
    """The (first cell, second cell) pairs of the table under `heading`."""
    body = text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2 or set(cells[0]) <= {"-", " "}:
            continue
        if cells[0] == "Module":  # header row
            continue
        rows.append((cells[0], cells[1]))
    return rows


def _module_name(cell: str) -> str | None:
    """The module a first cell names, or None for the prose entries.

    The index also routes a handful of cross-module topics that have notes but
    no single file -- "Startup: BASIC clear-and-loop program", "Composable
    scenes". Those are rows, not modules.
    """
    match = re.fullmatch(r"`([\w./]+)`", cell)
    if match and (match.group(1).endswith(".py") or match.group(1).endswith("/")):
        return match.group(1)
    return None


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for match in re.finditer(r"^#+\s+(.*?)\s*$", path.read_text(encoding="utf-8"), re.M):
        slug = _bookdoc.heading_slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        out.add(slug if count == 0 else f"{slug}-{count}")
    return out


class ArchitectureIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _INDEX.read_text(encoding="utf-8")
        self.indexed = _table_rows(self.text, "Module index")
        self.uncovered = _table_rows(self.text, "Not covered here")

    def test_every_module_is_listed_exactly_once(self) -> None:
        listed: list[str] = []
        for cell, _ in self.indexed + self.uncovered:
            name = _module_name(cell)
            if name is not None:
                listed.append(name)
        duplicates = {name for name in listed if listed.count(name) > 1}
        self.assertEqual(set(), duplicates, "listed twice in docs/architecture.md")

        modules = _modules()
        missing = sorted(modules - set(listed))
        self.assertEqual(
            [],
            missing,
            "new modules with no index row: add one pointing at their notes, or "
            'list them under "Not covered here"',
        )
        stale = sorted(set(listed) - modules)
        self.assertEqual([], stale, "index rows for modules that no longer exist")

    def test_every_index_link_resolves(self) -> None:
        for cell, target in self.indexed:
            with self.subTest(module=cell):
                match = re.search(r"\]\(([^)]+)\)", target)
                self.assertIsNotNone(match, f"{cell} has no link")
                assert match is not None
                relative, _, fragment = match.group(1).partition("#")
                path = (_INDEX.parent / relative).resolve()
                self.assertTrue(path.is_file(), f"{cell} points at a missing file")
                self.assertIn(
                    fragment,
                    _anchors(path),
                    f"{cell} points at an anchor {path.name} does not have",
                )

    def test_uncovered_modules_carry_their_rationale_in_a_docstring(self) -> None:
        """The list promises a docstring; a bare module makes it a dead end."""
        for cell, _ in self.uncovered:
            name = _module_name(cell)
            if name is None or name.endswith("/"):
                continue
            with self.subTest(module=name):
                source = (_PACKAGE / name).read_text(encoding="utf-8")
                docstring = ast.get_docstring(ast.parse(source))
                if name == "__main__.py":
                    continue  # a three-line entry point has nothing to say
                self.assertTrue(
                    docstring,
                    "listed as uncovered but has no module docstring either",
                )


if __name__ == "__main__":
    unittest.main()
