"""Tests for the web console's media browser + uploader, and its root jail.

Modeled on tests/test_config_store.py: the jail (symlink escape, a non-
directory root dropped rather than fatal) is the part worth testing hardest,
plus the listing behavior specific to this module — kind filtering, a
directory offered as an entry, `q`, truncation, and spec round-tripping for a
`~`-spelled root — and, since this module grew a write side, `destination`'s
own policy (bad names, an unconfigured kind, the off switch) and `receive`'s
streamed commit (landing bytes, never overwriting, cleaning up after itself).

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
            store = media_store.MediaStore(
                read_only=[str(self.assets), str(self.tmp / "nope")], cwd=self.tmp
            )
        self.assertEqual([r.path for r in store.roots], [self.assets])

    def test_the_same_root_twice_is_listed_once(self):
        # `cwd=self.tmp`: an unmentioned write kind still falls back to its
        # default directory, and a bare `MediaStore()` would resolve that
        # default against the real process cwd — this very repository, which
        # happens to ship real `assets/videos` etc. under it.
        with quiet_logging():
            store = media_store.MediaStore(
                read_only=[str(self.assets), str(self.assets)], cwd=self.tmp
            )
        self.assertEqual(len(store.roots), 1)

    def test_empty_write_table_defaults_to_the_four_asset_dirs(self):
        # Relative to cwd, same as the loader's own unset-`file =` default —
        # exercised here via an explicit `cwd` so the test doesn't depend on
        # the process's actual working directory. Only one of the four
        # defaults exists in this fixture, so the other three log a
        # dropped-root warning — incidental to what this test checks.
        (self.tmp / "assets" / "videos").mkdir()
        with quiet_logging():
            store = media_store.MediaStore(cwd=self.tmp)
        self.assertIn(self.tmp / "assets" / "videos", [r.path for r in store.roots])

    def test_a_root_spelled_with_a_tilde_keeps_that_spelling_in_specs(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / "clip.mp4").write_bytes(b"")
        # `os.path.expanduser` reads $HOME on POSIX and $USERPROFILE first on
        # Windows (falling back to $HOMEDRIVE+$HOMEPATH, then $HOME) — both
        # are set so this is deterministic on every CI runner.
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            with quiet_logging():
                store = media_store.MediaStore(read_only=["~"], cwd=self.tmp)
            out = store.index("video")
        specs = {e["spec"] for e in out["entries"]}
        self.assertEqual(specs, {"~", "~/clip.mp4"})


class WritableTest(StoreTestCase):
    def test_a_write_table_root_is_writable(self):
        videos = self.assets / "videos"
        videos.mkdir()
        with quiet_logging():
            store = media_store.MediaStore(read_write={"video": "assets/videos"}, cwd=self.tmp)
        self.assertTrue(store.roots[0].writable)

    def test_a_read_only_root_is_not_writable(self):
        with quiet_logging():
            store = media_store.MediaStore(read_only=[str(self.assets)], cwd=self.tmp)
        self.assertFalse(store.roots[0].writable)

    def test_the_same_path_in_both_lists_keeps_its_writable_root(self):
        # Write roots resolve first, so a path named in both lists ends up
        # `writable` — "write paths first" in the docstring is also why an
        # upload's own directory sorts to the front of a listing.
        with quiet_logging():
            store = media_store.MediaStore(
                read_write={"video": str(self.assets)},
                read_only=[str(self.assets)],
                cwd=self.tmp,
            )
        self.assertEqual([r.path for r in store.roots], [self.assets])
        self.assertTrue(store.roots[0].writable)


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
        # `cwd=self.tmp` keeps the write table's own default kinds (unset here)
        # from resolving against the real process cwd; none of their default
        # directories exist under `self.tmp`, so quiet_logging swallows the
        # dropped-root warnings that follow from that.
        with quiet_logging():
            self.store = media_store.MediaStore(read_only=["assets"], cwd=self.tmp)

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
        with quiet_logging():
            store = media_store.MediaStore(read_only=[str(self.assets)], cwd=self.tmp)
        specs = [e["spec"] for e in store.index("video")["entries"] if not e["is_dir"]]
        self.assertEqual(specs, [])


class TruncationTest(StoreTestCase):
    def test_max_files_sets_truncated(self):
        for i in range(media_store.MAX_FILES + 5):
            (self.assets / f"clip{i}.mp4").write_bytes(b"")
        with quiet_logging():
            store = media_store.MediaStore(read_only=[str(self.assets)], cwd=self.tmp)
        out = store.index("video")
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["entries"]), media_store.MAX_FILES)


class DestinationTest(StoreTestCase):
    """`cwd=self.tmp` on every store built here, even the ones that only name
    `video` — an unmentioned kind still falls back to its default directory
    (`_resolve_write_table`), and a bare `MediaStore()` would resolve that
    default against the *real* process cwd, which is this very repository and
    happens to have real `assets/sids` etc. under it. Not naming `cwd` would
    make these tests read the checkout they're running from."""

    def setUp(self) -> None:
        super().setUp()
        self.videos = self.assets / "videos"
        self.videos.mkdir()
        with quiet_logging():
            self.store = media_store.MediaStore(read_write={"video": "assets/videos"}, cwd=self.tmp)

    def test_picks_the_kind_off_the_extension(self):
        self.assertEqual(self.store.destination("clip.mp4"), ("video", self.videos))

    def test_rejects_a_name_with_a_forward_slash(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination("sub/clip.mp4")

    def test_rejects_a_name_with_a_backslash(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination("sub\\clip.mp4")

    def test_rejects_dot_dot(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination("..")

    def test_rejects_a_leading_dot(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination(".clip.mp4")

    def test_rejects_an_empty_name(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination("")

    def test_rejects_an_overlong_name(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination(("x" * 300) + ".mp4")

    def test_rejects_an_extension_no_kind_claims(self):
        with self.assertRaises(media_store.MediaNameRejected):
            self.store.destination("readme.txt")

    def test_refuses_a_kind_with_no_default_destination(self):
        # "audio" has no default directory at all (generative's own
        # `audio_source = "file"` requires an explicit `file =`).
        with self.assertRaises(media_store.MediaNotUploadable):
            self.store.destination("song.mp3")

    def test_refuses_a_kind_set_to_the_empty_string(self):
        with quiet_logging():
            store = media_store.MediaStore(read_write={"video": ""}, cwd=self.tmp)
        with self.assertRaises(media_store.MediaNotUploadable):
            store.destination("clip.mp4")

    def test_turning_one_kind_off_leaves_the_others_at_their_default(self):
        sids = self.tmp / "assets" / "sids"
        sids.mkdir()
        with quiet_logging():
            store = media_store.MediaStore(read_write={"video": ""}, cwd=self.tmp)
        self.assertEqual(store.destination("tune.sid"), ("sid", sids))


class ReceiveTest(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.videos = self.assets / "videos"
        self.videos.mkdir()
        with quiet_logging():
            self.store = media_store.MediaStore(read_write={"video": "assets/videos"}, cwd=self.tmp)

    def test_receive_lands_the_bytes_and_answers_the_spec(self):
        with self.store.receive("clip.mp4") as upload:
            upload.write(b"hello ")
            upload.write(b"world")
        self.assertEqual((self.videos / "clip.mp4").read_bytes(), b"hello world")
        self.assertEqual(
            upload.result,
            {
                "spec": "assets/videos/clip.mp4",
                "name": "clip.mp4",
                "kind": "video",
                "bytes": 11,
                "renamed": False,
            },
        )

    def test_a_second_upload_of_the_same_name_is_renamed_and_the_first_is_untouched(self):
        with self.store.receive("clip.mp4") as first:
            first.write(b"first")
        with self.store.receive("clip.mp4") as second:
            second.write(b"second")
        self.assertEqual((self.videos / "clip.mp4").read_bytes(), b"first")
        self.assertEqual((self.videos / "clip-2.mp4").read_bytes(), b"second")
        self.assertEqual(second.result["name"], "clip-2.mp4")
        self.assertTrue(second.result["renamed"])

    def test_an_exception_mid_stream_leaves_no_part_file_and_no_target(self):
        with self.assertRaises(RuntimeError):
            with self.store.receive("clip.mp4") as upload:
                upload.write(b"partial")
                raise RuntimeError("boom")
        self.assertEqual(list(self.videos.iterdir()), [])

    def test_past_the_cap_raises_and_leaves_nothing(self):
        with mock.patch.object(media_store, "MAX_UPLOAD_BYTES", 4):
            with self.assertRaises(media_store.MediaTooLarge):
                with self.store.receive("clip.mp4") as upload:
                    upload.write(b"way too big")
        self.assertEqual(list(self.videos.iterdir()), [])

    def test_too_many_collisions_is_refused_rather_than_renamed_forever(self):
        with mock.patch.object(media_store, "_MAX_RENAME_ATTEMPTS", 2):
            with self.store.receive("clip.mp4") as upload:
                upload.write(b"a")
            with self.store.receive("clip.mp4") as upload:
                upload.write(b"b")
            with self.assertRaises(media_store.MediaNameRejected):
                with self.store.receive("clip.mp4"):
                    pass
        self.assertEqual(sorted(p.name for p in self.videos.iterdir()), ["clip-2.mp4", "clip.mp4"])


if __name__ == "__main__":
    unittest.main()
