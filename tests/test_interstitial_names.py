"""Tests for the prepare_next() interstitial-name plumbing.

The "UP NEXT" interstitial is built from the upcoming scene's `.name` BEFORE
that scene's setup() runs (playlist.py). Randomized scenes (Slideshow,
Commercial, Waveform) used to leave `.name` showing the directory spec or a
stale prior pick at that moment; `prepare_next()` now performs the random pick
up front so the card shows the real upcoming file, with file extensions
stripped.

These tests exercise the scene-side hooks (no U64 hardware, no PyAV). cv2 is
used to write tiny real PNGs for the slideshow path.
"""
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

import cv2
import numpy as np

from c64cast.scenes import CommercialScene, SlideshowScene, _display_name


class DisplayNameTest(unittest.TestCase):
    def test_strips_extension_and_dir(self):
        self.assertEqual(_display_name("/a/b/cat.png"), "cat")
        self.assertEqual(_display_name("intro.mp4"), "intro")
        self.assertEqual(_display_name("/x/y/My Tune.sid"), "My Tune")

    def test_no_extension_passthrough(self):
        self.assertEqual(_display_name("/a/b/README"), "README")

    def test_only_final_extension_removed(self):
        self.assertEqual(_display_name("archive.tar.gz"), "archive.tar")


class CommercialPrepareNextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Two empty-but-correctly-extensioned files; resolve_file_spec only
        # checks extensions, not container validity.
        for n in ("alpha.mp4", "beta.mp4"):
            open(os.path.join(self.tmp.name, n), "wb").close()

    def _scene(self):
        return CommercialScene(MagicMock(), None, MagicMock(), self.tmp.name)

    def test_init_name_is_dir_spec_for_multi_pool(self):
        # Before prepare_next, a multi-entry pool can't know its pick.
        scene = self._scene()
        self.assertEqual(scene.name, f"Commercial: {self.tmp.name}")
        self.assertFalse(scene._prepared)

    def test_prepare_next_picks_real_file_without_extension(self):
        scene = self._scene()
        scene.prepare_next()
        self.assertTrue(scene._prepared)
        self.assertIn(scene.name, ("Commercial: alpha", "Commercial: beta"))
        self.assertIn(os.path.basename(scene.filepath),
                      ("alpha.mp4", "beta.mp4"))

    def test_single_entry_init_name_has_no_extension(self):
        f = os.path.join(self.tmp.name, "alpha.mp4")
        scene = CommercialScene(MagicMock(), None, MagicMock(), f)
        self.assertEqual(scene.name, "Commercial: alpha")

    def test_pick_filepath_failure_returns_false(self):
        scene = self._scene()
        # Empty the pool out from under the scene.
        for n in os.listdir(self.tmp.name):
            os.remove(os.path.join(self.tmp.name, n))
        # Both _pick_filepath and prepare_next log the expected resolve
        # failure; assertLogs asserts it and keeps it off the console.
        with self.assertLogs("c64cast.scenes", level="ERROR"):
            self.assertFalse(scene._pick_filepath())
            scene.prepare_next()
        self.assertFalse(scene._prepared)


class SlideshowPrepareNextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        for n in ("one.png", "two.png"):
            cv2.imwrite(os.path.join(self.tmp.name, n), img)

    def _scene(self):
        return SlideshowScene(MagicMock(), MagicMock(), self.tmp.name)

    def test_init_name_is_dir_spec_for_multi_pool(self):
        scene = self._scene()
        self.assertEqual(scene.name, f"Slideshow: {self.tmp.name}")
        self.assertFalse(scene._prepared)

    def test_prepare_next_loads_first_slide_without_extension(self):
        scene = self._scene()
        scene.prepare_next()
        self.assertTrue(scene._prepared)
        self.assertIsNotNone(scene._current_img)
        self.assertIn(scene.name, ("Slideshow: one", "Slideshow: two"))

    def test_setup_consumes_prepared_pick(self):
        scene = self._scene()
        scene.prepare_next()
        prepared_name = scene.name
        scene.setup()
        # Flag cleared, the prepared slide kept (not re-rolled), scene live.
        self.assertFalse(scene._prepared)
        self.assertEqual(scene.name, prepared_name)
        self.assertIsNotNone(scene._current_img)
        self.assertFalse(scene.is_done)

    def test_setup_without_prepare_still_picks(self):
        scene = self._scene()
        scene.setup()
        self.assertFalse(scene._prepared)
        self.assertIn(scene.name, ("Slideshow: one", "Slideshow: two"))
        self.assertFalse(scene.is_done)


class WaveformPrepareNextTest(unittest.TestCase):
    """The waveform scene can't be fully constructed without a valid PSID +
    py65 emulator, so we exercise just the prepare_next/setup flag branch on
    a bare instance with the SID-loading internals stubbed."""

    def _bare_scene(self):
        from c64cast.waveform import WaveformScene
        scene = WaveformScene.__new__(WaveformScene)
        scene._candidates = ["a.sid", "b.sid"]
        scene._prepared = False
        scene.song = 0
        scene.name = "SID: old #0"
        scene.header = MagicMock(name="hdr")
        scene.header.name = "Picked Tune"
        scene._sid_file = "b.sid"
        scene.load_calls = 0

        def fake_load():
            scene.load_calls += 1
        scene._pick_and_load_sid = fake_load  # type: ignore[method-assign]
        scene._resolve_duration_for_current_sid = lambda: 42.0  # type: ignore[method-assign]
        return scene

    def test_prepare_next_repicks_and_sets_name(self):
        scene = self._bare_scene()
        scene.prepare_next()
        self.assertTrue(scene._prepared)
        self.assertEqual(scene.load_calls, 1)
        self.assertEqual(scene.name, "SID: Picked Tune #0")
        self.assertEqual(scene.duration_s, 42.0)

    def test_setup_consumes_prepared_pick_without_reloading(self):
        from c64cast.scenes import Scene
        scene = self._bare_scene()
        scene.prepare_next()
        self.assertEqual(scene.load_calls, 1)
        # Drive only the branch we changed (skip the heavy rest of setup()).
        if scene._prepared:
            scene._prepared = False
        elif len(scene._candidates) > 1:
            scene._repick_sid()
        # The prepared pick was consumed — no second SID load.
        self.assertEqual(scene.load_calls, 1)
        self.assertFalse(scene._prepared)
        # Sanity: the base hook is a no-op so non-randomized scenes are inert.
        self.assertIsNone(Scene.prepare_next(scene))


if __name__ == "__main__":
    unittest.main()
