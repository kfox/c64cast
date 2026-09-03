"""Tests for c64cast.app.fs_walk — the depth-capped, skip-dirs-pruned walk
shared by config_store and media_store's root jails, and the `base-2`,
`base-3`, … name disambiguator both plug their own `taken` check into.

Neither module's own test file exercises `walk_dirs` directly — both only
ever see it through a caller's own filtering — so the depth cap and
skip-dirs pruning this module exists to centralize had no test of their own
before this file. The per-entry symlink-escape re-check each caller runs on
top of the walk is covered where the caller is: tests/test_config_store.py
and tests/test_media_store.py.

Not covered: the per-`os.walk` platform quirks (case-insensitive
filesystems, junctions) — `walk_dirs` doesn't try to normalize those,
matching its callers' own scope."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c64cast.app import fs_walk


class WalkDirsDepthTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_a_directory_past_max_depth_is_never_descended_into(self):
        with mock.patch.object(fs_walk, "MAX_DEPTH", 2):
            deep = self.root
            for i in range(4):
                deep = deep / f"level{i}"
                deep.mkdir()
            visited = {
                here.relative_to(self.root).as_posix() for here, _ in fs_walk.walk_dirs(self.root)
            }
        self.assertEqual(visited, {".", "level0", "level0/level1"})

    def test_a_file_at_the_depth_cap_itself_is_still_yielded(self):
        # The cap prunes *descent* past a directory at MAX_DEPTH; that
        # directory's own filenames are still yielded — only its children
        # are cut off.
        with mock.patch.object(fs_walk, "MAX_DEPTH", 1):
            sub = self.root / "level0"
            sub.mkdir()
            (sub / "here.txt").write_bytes(b"")
            results = dict(fs_walk.walk_dirs(self.root))
        self.assertEqual(results[sub], ["here.txt"])


class WalkDirsSkipDirsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_skip_dirs_and_dot_directories_are_never_descended_into(self):
        for name in (*fs_walk.SKIP_DIRS, ".hidden"):
            sub = self.root / name
            sub.mkdir()
            (sub / "secret.txt").write_bytes(b"")
        visited = {here for here, _ in fs_walk.walk_dirs(self.root)}
        self.assertEqual(visited, {self.root})


class DisambiguateTest(unittest.TestCase):
    def test_a_free_name_is_returned_unchanged(self):
        name, renamed = fs_walk.disambiguate("clip", ".mp4", lambda c: False)
        self.assertEqual((name, renamed), ("clip.mp4", False))

    def test_one_collision_numbers_from_two(self):
        taken = {"clip.mp4"}
        name, renamed = fs_walk.disambiguate("clip", ".mp4", lambda c: c in taken)
        self.assertEqual((name, renamed), ("clip-2.mp4", True))

    def test_max_attempts_bounds_the_search(self):
        with self.assertRaises(LookupError):
            fs_walk.disambiguate("clip", ".mp4", lambda c: True, max_attempts=2)


if __name__ == "__main__":
    unittest.main()
