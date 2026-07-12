"""Scene fade-in/out: the palette dim LUT, per-mode buffer fade, and the
Playlist timeline + CTRL-skip abort.

The fade dims a composed frame's color-bearing fields toward black via a
palette remap (palette.build_fade_lut), leaving the bitmap pixel-selectors
alone, so it works uniformly across the compose-based display modes. The
Playlist drives fade_alpha 0→1 on entry, freezes+dims the last frame on a
normal end, and aborts either fade the instant a CTRL skip arrives.
"""

# FakeAPI / fake display mode are duck-typed boundary stubs.
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast.modes import (  # noqa: E402
    HiresDisplayMode,
    MCMDisplayMode,
    MultiHiresDisplayMode,
    PETSCIIDisplayMode,
    _fade_nibbles,
)
from c64cast.palette import build_fade_lut  # noqa: E402
from c64cast.playlist import Playlist  # noqa: E402


def _frame() -> np.ndarray:
    """Deterministic synthetic BGR gradient (200x320), vivid enough that every
    mode picks non-black colors."""
    h, w = 200, 320
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    b = xx / w * 255
    g = yy / h * 255
    r = (xx + yy) / (h + w) * 255
    return np.clip(np.stack([b, g, r], axis=-1), 0, 255).astype(np.uint8)


class FadeLutTest(unittest.TestCase):
    def test_identity_at_full_brightness(self):
        self.assertEqual(build_fade_lut(1.0).tolist(), list(range(16)))
        self.assertEqual(build_fade_lut(2.0).tolist(), list(range(16)))

    def test_all_black_at_zero(self):
        self.assertEqual(build_fade_lut(0.0).tolist(), [0] * 16)
        self.assertEqual(build_fade_lut(-1.0).tolist(), [0] * 16)

    def test_black_stays_black(self):
        for a in (0.0, 0.25, 0.5, 0.75, 1.0):
            self.assertEqual(int(build_fade_lut(a)[0]), 0, f"black drifted at alpha={a}")

    def test_dimmed_is_never_brighter(self):
        # Each remapped color's luma must not exceed the original's: a fade
        # toward black can hold or darken, never brighten.
        from c64cast.palette import C64_PALETTE_BGR

        luma = C64_PALETTE_BGR.sum(axis=1)
        lut = build_fade_lut(0.5)
        for c in range(16):
            self.assertLessEqual(luma[lut[c]], luma[c] + 1e-6, f"palette {c} brightened under fade")

    def test_constrained_set_stays_in_range(self):
        # MCM foreground is palette 0..7 only (color RAM bit 3 = multicolor flag).
        for a in (0.0, 0.3, 0.6, 0.9):
            lut = build_fade_lut(a, allowed=tuple(range(8)))
            self.assertTrue(all(0 <= v <= 7 for v in lut.tolist()), f"out of range at {a}")


class FadeNibbleTest(unittest.TestCase):
    def test_both_nibbles_mapped_independently(self):
        lut = build_fade_lut(0.5)
        # hi=white(1), lo=red(2) packed as one screen byte per cell.
        arr = np.array([(1 << 4) | 2, (15 << 4) | 0], dtype=np.uint8)
        out = _fade_nibbles(arr, lut)
        self.assertEqual(int(out[0] >> 4), int(lut[1]))
        self.assertEqual(int(out[0] & 0x0F), int(lut[2]))
        self.assertEqual(int(out[1] >> 4), int(lut[15]))
        self.assertEqual(int(out[1] & 0x0F), int(lut[0]))


class ApplyFadeTest(unittest.TestCase):
    """apply_fade dims color-bearing fields and never touches the bitmap, nor
    mutates the input buffers (the fade-out replays from the pristine cache)."""

    def test_petscii_dims_color_only(self):
        mode = PETSCIIDisplayMode("default")
        api = FakeAPI()
        mode.setup(api)
        buffers = mode.compose(_frame())
        original_color = buffers["color"].copy()
        original_screen = buffers["screen"].copy()
        mode.fade_alpha = 0.4
        out = mode.apply_fade(buffers)
        # screen holds glyph codes, not colors — untouched.
        self.assertTrue(np.array_equal(out["screen"], original_screen))
        # color dimmed (and input buffer left pristine for replay).
        self.assertFalse(np.array_equal(out["color"], original_color))
        self.assertTrue(np.array_equal(buffers["color"], original_color))

    def test_mcm_keeps_multicolor_flag_and_fg_range(self):
        mode = MCMDisplayMode("vivid")
        api = FakeAPI()
        mode.setup(api)
        buffers = mode.compose(_frame())
        mode.fade_alpha = 0.5
        out = mode.apply_fade(buffers)
        # Every color-RAM byte must keep bit 3 set (multicolor) and a legal fg.
        self.assertTrue(np.all((out["color"] & 0x08) == 0x08))
        self.assertTrue(np.all((out["color"] & 0x07) <= 7))
        # Shared bg registers dimmed too.
        self.assertEqual(len(out["bg"]), 3)

    def test_bitmap_modes_leave_bitmap_untouched(self):
        for mode in (HiresDisplayMode("normal"), MultiHiresDisplayMode("percell")):
            with self.subTest(mode=mode.name):
                api = FakeAPI()
                mode.setup(api)
                buffers = mode.compose(_frame())
                original_bitmap = buffers["bitmap"].copy()
                original_screen = buffers["screen"].copy()
                mode.fade_alpha = 0.5
                out = mode.apply_fade(buffers)
                # bitmap = pixel selectors — must be byte-identical.
                self.assertTrue(np.array_equal(out["bitmap"], original_bitmap))
                # screen (packed color nibbles) dimmed.
                self.assertFalse(np.array_equal(out["screen"], original_screen))

    def test_full_brightness_is_noop(self):
        mode = HiresDisplayMode("normal")
        api = FakeAPI()
        mode.setup(api)
        buffers = mode.compose(_frame())
        mode.fade_alpha = 1.0
        out = mode.apply_fade(buffers)
        self.assertTrue(np.array_equal(out["screen"], buffers["screen"]))


class RepushFadedTest(unittest.TestCase):
    def test_repush_uses_cached_buffers(self):
        mode = HiresDisplayMode("normal")
        api = FakeAPI()
        mode.setup(api)
        mode._last_buffers = mode.compose(_frame())
        api.regions.clear()
        mode.repush_faded(api, 0.0)  # fully black
        # A full re-push happened (bitmap + screen regions written).
        self.assertIn(0x2000, api.regions)
        self.assertIn(0x0400, api.regions)
        # fade_alpha restored to its prior value after the push.
        self.assertEqual(mode.fade_alpha, 1.0)

    def test_repush_noop_without_cache(self):
        mode = HiresDisplayMode("normal")
        api = FakeAPI()
        mode._last_buffers = None
        mode.repush_faded(api, 0.5)
        self.assertEqual(api.regions, {})


class UserDimTest(unittest.TestCase):
    """user_dim (the WLED `bri` slider as a real dim) dims exactly like a fade,
    folds multiplicatively with fade_alpha, and is identity at 1.0."""

    def test_user_dim_alone_dims_like_equivalent_fade(self):
        # user_dim=0.5 at full fade_alpha must dim identically to fade_alpha=0.5
        # with user_dim=1.0 — they both land as build_fade_lut(0.5).
        for cls, style in ((PETSCIIDisplayMode, "default"), (MultiHiresDisplayMode, "percell")):
            with self.subTest(mode=cls.__name__):
                a, b = cls(style), cls(style)
                api = FakeAPI()
                a.setup(api)
                b.setup(api)
                frame = _frame()
                a.user_dim = 0.5
                b.fade_alpha = 0.5
                out_dim = a.apply_fade(a.compose(frame))
                out_fade = b.apply_fade(b.compose(frame))
                self.assertTrue(np.array_equal(out_dim["color"], out_fade["color"]))
                # And a genuine dim did happen (not a no-op that trivially matches).
                self.assertFalse(np.array_equal(out_dim["color"], a.compose(frame)["color"]))

    def test_product_equals_single_combined_alpha(self):
        # fade_alpha=0.5 × user_dim=0.5 must equal one fade at 0.25.
        mode = PETSCIIDisplayMode("default")
        api = FakeAPI()
        mode.setup(api)
        buffers = mode.compose(_frame())
        mode.fade_alpha = 0.5
        mode.user_dim = 0.5
        self.assertAlmostEqual(mode._fade_lut_alpha, 0.25)
        out = mode.apply_fade(buffers)
        expected = build_fade_lut(0.25)[buffers["color"]]
        self.assertTrue(np.array_equal(out["color"], expected))

    def test_user_dim_identity_at_one(self):
        mode = HiresDisplayMode("normal")
        api = FakeAPI()
        mode.setup(api)
        buffers = mode.compose(_frame())
        mode.user_dim = 1.0  # (already the default)
        out = mode.apply_fade(buffers)
        self.assertTrue(np.array_equal(out["screen"], buffers["screen"]))

    def test_repush_folds_user_dim_and_preserves_it(self):
        # A fade-out replay at alpha=0.6 while user_dim=0.5 pushes the frame
        # dimmed to the product 0.3; user_dim survives, fade_alpha is restored.
        mode = HiresDisplayMode("normal")
        api = FakeAPI()
        mode.setup(api)
        cached = mode.compose(_frame())
        mode._last_buffers = cached
        mode.user_dim = 0.5
        expected_screen = _fade_nibbles(cached["screen"], build_fade_lut(0.6 * 0.5)).tobytes()
        api.regions.clear()
        mode.repush_faded(api, 0.6)
        self.assertEqual(api.regions[0x0400], expected_screen)
        self.assertEqual(mode.user_dim, 0.5)
        self.assertEqual(mode.fade_alpha, 1.0)


# ---------------------------------------------------------------------------
# Playlist timeline + CTRL-skip abort
# ---------------------------------------------------------------------------


class _FakeMode:
    """Minimal compose-based display mode stand-in for Playlist fade wiring."""

    name = "fake"
    supports_compose = True

    def __init__(self):
        self.fade_alpha = 1.0
        self._last_buffers = object()  # non-None: a frame was "composed"
        self.repush_calls: list[float] = []

    def repush_faded(self, api, alpha):
        self.repush_calls.append(alpha)


class _FakeScene:
    def __init__(self, name="A"):
        self.name = name
        self.is_done = False
        self.target_fps = None
        self.display_mode = _FakeMode()


def _playlist(scene, *, fade_duration_s=0.05, fps=200.0):
    api = type("Api", (), {"stats": {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}})()
    return Playlist(
        [scene, _FakeScene("B")],
        api,
        target_fps=fps,
        heartbeat_interval=0.0,
        stop_event=threading.Event(),
        interstitial_factory=lambda name: _FakeScene(f"trans:{name}"),
        fade_duration_s=fade_duration_s,
    )


class PlaylistFadeInTest(unittest.TestCase):
    def test_begin_fade_in_starts_black_and_ramps(self):
        s = _FakeScene()
        pl = _playlist(s)
        pl._begin_fade_in(s)
        self.assertEqual(s.display_mode.fade_alpha, 0.0)
        n = pl._fade_in_remaining
        self.assertGreater(n, 0)
        # Stepping n frames ramps fade_alpha monotonically up to exactly 1.0.
        last = 0.0
        for _ in range(n):
            pl._advance_fade_in(s)
            self.assertGreaterEqual(s.display_mode.fade_alpha, last)
            last = s.display_mode.fade_alpha
        self.assertEqual(last, 1.0)
        self.assertEqual(pl._fade_in_remaining, 0)

    def test_disabled_when_duration_zero(self):
        s = _FakeScene()
        pl = _playlist(s, fade_duration_s=0.0)
        pl._begin_fade_in(s)
        self.assertEqual(s.display_mode.fade_alpha, 1.0)
        self.assertEqual(pl._fade_in_remaining, 0)

    def test_cancel_fade_in_snaps_to_full(self):
        s = _FakeScene()
        pl = _playlist(s)
        pl._begin_fade_in(s)
        pl._cancel_fade_in(s)
        self.assertEqual(s.display_mode.fade_alpha, 1.0)
        self.assertEqual(pl._fade_in_remaining, 0)


class PlaylistFadeOutTest(unittest.TestCase):
    def test_fade_out_dims_to_black_over_n_frames(self):
        s = _FakeScene()
        pl = _playlist(s)
        pl._fade_out(s)
        n = pl._fade_frames(s)
        self.assertEqual(len(s.display_mode.repush_calls), n)
        # alphas descend toward (and reach) 0, then mode left at full brightness.
        self.assertAlmostEqual(s.display_mode.repush_calls[-1], 0.0)
        self.assertEqual(s.display_mode.fade_alpha, 1.0)

    def test_skip_before_fade_out_suppresses_it(self):
        s = _FakeScene()
        pl = _playlist(s)
        pl._ended_via_skip = True
        pl._fade_out(s)
        self.assertEqual(s.display_mode.repush_calls, [])

    def test_skip_during_fade_out_aborts_and_consumes_event(self):
        s = _FakeScene()
        pl = _playlist(s)
        # Set the skip event after the 2nd dim push: the loop must break on the
        # next iteration and clear the event (so it doesn't skip scene B too).
        real = s.display_mode.repush_faded

        def spy(api, alpha):
            real(api, alpha)
            if len(s.display_mode.repush_calls) == 2:
                pl.skip_event.set()

        s.display_mode.repush_faded = spy
        pl._fade_out(s)
        self.assertEqual(len(s.display_mode.repush_calls), 2)
        self.assertFalse(pl.skip_event.is_set(), "skip must be consumed by the aborted fade")

    def test_fade_out_noop_without_rendered_frame(self):
        s = _FakeScene()
        s.display_mode._last_buffers = None
        pl = _playlist(s)
        pl._fade_out(s)
        self.assertEqual(s.display_mode.repush_calls, [])


if __name__ == "__main__":
    unittest.main()
