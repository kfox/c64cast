"""Tests for the web console's read-only media browser and its root jail.

Modeled on tests/test_config_store.py: the jail (symlink escape, a non-
directory root dropped rather than fatal) is the part worth testing hardest,
plus the listing behavior specific to this module — kind filtering, a
directory offered as an entry, `q`, truncation, and spec round-tripping for a
`~`-spelled root.

Not covered: two roots whose kind's extensions overlap producing duplicate
specs (they wouldn't — a spec is root-relative and roots are deduplicated by
resolved path) and a case-insensitive filesystem collision (config_store's
own test module leaves the same case)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fakes import quiet_logging

from c64cast.app import media_store


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved because macOS hands out /var/folders/... symlinks for the
        # temp dir, and every path this module returns is real.
        self.tmp = Path(tmp.name).resolve()
        self.assets = self.tmp / "assets"
        self.assets.mkdir()


class SpecTest(unittest.TestCase):
    def test_a_root_spelled_as_only_slashes_specs_from_the_root_not_cwd(self):
        # `rstrip("/")` on a spelling of just "/" empties out entirely; the
        # fallback has to land back on "/", not "." (which would silently
        # point a listed spec at the process's cwd instead of the filesystem
        # root).
        root = media_store.MediaRoot(spelling="/", path=Path("/"))
        self.assertEqual(media_store._spec(root, ("etc", "motd")), "/etc/motd")


class RootsTest(StoreTestCase):
    def test_a_root_that_is_not_a_directory_is_dropped_not_fatal(self):
        with self.assertLogs("c64cast.app.media_store", level="WARNING"):
            store = media_store.MediaStore([str(self.assets), str(self.tmp / "nope")])
        self.assertEqual([r.path for r in store.roots], [self.assets])

    def test_the_same_root_twice_is_listed_once(self):
        store = media_store.MediaStore([str(self.assets), str(self.assets)])
        self.assertEqual(len(store.roots), 1)

    def test_empty_roots_default_to_the_four_asset_dirs(self):
        # Relative to cwd, same as the loader's own unset-`file =` default —
        # exercised here via an explicit `cwd` so the test doesn't depend on
        # the process's actual working directory. Only one of the four
        # defaults exists in this fixture, so the other three log a
        # dropped-root warning — incidental to what this test checks.
        (self.tmp / "assets" / "videos").mkdir()
        with quiet_logging():
            store = media_store.MediaStore([], cwd=self.tmp)
        self.assertIn(self.tmp / "assets" / "videos", [r.path for r in store.roots])

    def test_a_root_spelled_with_a_tilde_keeps_that_spelling_in_specs(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / "clip.mp4").write_bytes(b"")
        # `os.path.expanduser` reads $HOME on POSIX and $USERPROFILE first on
        # Windows (falling back to $HOMEDRIVE+$HOMEPATH, then $HOME) — both
        # are set so this is deterministic on every CI runner.
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            store = media_store.MediaStore(["~"])
            out = store.index("video")
        specs = {e["spec"] for e in out["entries"]}
        self.assertEqual(specs, {"~", "~/clip.mp4"})


class ListingTest(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.assets / "clip.mp4").write_bytes(b"x")
        (self.assets / "tune.sid").write_bytes(b"y")
        (self.assets / "readme.txt").write_bytes(b"z")
        sub = self.assets / "more"
        sub.mkdir()
        (sub / "another.mp4").write_bytes(b"w")
        # Root spelled relative to `cwd`, so specs come out as `assets/...`
        # rather than an absolute path — the spelling a saved config would use.
        self.store = media_store.MediaStore(["assets"], cwd=self.tmp)

    def test_only_the_kinds_extensions_are_listed(self):
        specs = {e["spec"] for e in self.store.index("video")["entries"] if not e["is_dir"]}
        self.assertEqual(specs, {"assets/clip.mp4", "assets/more/another.mp4"})

    def test_a_directory_holding_a_match_is_its_own_entry(self):
        entries = self.store.index("video")["entries"]
        dirs = {e["spec"]: e["is_dir"] for e in entries if e["is_dir"]}
        self.assertEqual(dirs, {"assets": True, "assets/more": True})

    def test_a_directory_with_no_matching_kind_is_not_listed(self):
        # `more/` holds only a video; browsing for sid should not surface it.
        entries = self.store.index("sid")["entries"]
        self.assertEqual([e["spec"] for e in entries if e["is_dir"]], ["assets"])

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(media_store.MediaKindUnknown):
            self.store.index("subtitle")

    def test_q_filters_case_insensitively(self):
        specs = {e["spec"] for e in self.store.index("video", "ANOTHER")["entries"]}
        self.assertEqual(specs, {"assets/more/another.mp4"})

    def test_q_with_no_match_is_an_empty_list(self):
        self.assertEqual(self.store.index("video", "nope")["entries"], [])


class SymlinkEscapeTest(StoreTestCase):
    def test_a_symlinked_file_pointing_outside_its_root_is_not_listed(self):
        outside = self.tmp / "outside.mp4"
        outside.write_bytes(b"secret")
        (self.assets / "escape.mp4").symlink_to(outside)
        store = media_store.MediaStore([str(self.assets)])
        specs = [e["spec"] for e in store.index("video")["entries"] if not e["is_dir"]]
        self.assertEqual(specs, [])


class TruncationTest(StoreTestCase):
    def test_max_files_sets_truncated(self):
        for i in range(media_store.MAX_FILES + 5):
            (self.assets / f"clip{i}.mp4").write_bytes(b"")
        store = media_store.MediaStore([str(self.assets)])
        out = store.index("video")
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["entries"]), media_store.MAX_FILES)


if __name__ == "__main__":
    unittest.main()
