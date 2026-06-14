"""Tests for BigTextSpanOrchestrator — claims() dispatch, conductor-
must-be-rightmost validation, snapshot round-trip, and the per-system
window-slicing math."""

# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

import numpy as np

from c64cast.config import SceneCfg
from c64cast.ensemble import Ensemble, SystemStack
from c64cast.orchestrator import OrchestratorError
from c64cast.orchestrators.big_text_span import (
    SCREEN_W_PX,
    BigTextSpanOrchestrator,
)


def _fake_stack(name: str, scenes: list[SceneCfg] | None = None) -> SystemStack:
    cfg = MagicMock(name=f"cfg-{name}")
    cfg.scenes = scenes or []
    return SystemStack(
        name=name,
        cfg=cfg,
        api=MagicMock(name=f"api-{name}"),
        audio=None,
        source=None,
        playlist=MagicMock(name=f"playlist-{name}"),
        key_poller=MagicMock(name=f"keyboard-{name}"),
        framebuffer=None,
        preview_window=None,
        recorder=None,
    )


def _ensemble(*names: str) -> Ensemble:
    return Ensemble(stacks=[_fake_stack(n) for n in names], stop_event=threading.Event())


class ClaimsTest(unittest.TestCase):
    """claims() recognizes blank/mcm scenes carrying a big_text overlay
    and rejects everything else."""

    def test_claims_blank_with_big_text(self):
        cfg = SceneCfg(type="blank", name="b", overlays=[{"type": "big_text"}])
        self.assertTrue(BigTextSpanOrchestrator.claims(cfg))

    def test_claims_mcm_with_big_text(self):
        cfg = SceneCfg(type="mcm", name="b", overlays=[{"type": "big_text"}])
        self.assertTrue(BigTextSpanOrchestrator.claims(cfg))

    def test_rejects_webcam_with_big_text(self):
        # big_text isn't supported on webcam — claims() shouldn't lie
        # about it just because the overlay dict happens to be there.
        cfg = SceneCfg(type="webcam", name="b", overlays=[{"type": "big_text"}])
        self.assertFalse(BigTextSpanOrchestrator.claims(cfg))

    def test_rejects_blank_without_big_text(self):
        cfg = SceneCfg(type="blank", name="b", overlays=[{"type": "clock"}])
        self.assertFalse(BigTextSpanOrchestrator.claims(cfg))

    def test_rejects_blank_with_no_overlays(self):
        cfg = SceneCfg(type="blank", name="b")
        self.assertFalse(BigTextSpanOrchestrator.claims(cfg))


class RightmostConductorValidationTest(unittest.TestCase):
    def test_rightmost_conductor_accepted(self):
        ens = _ensemble("left", "middle", "right")
        orch = BigTextSpanOrchestrator(ens, "right")
        cfg = SceneCfg(type="blank", name="b")
        # Should not raise.
        self.assertTrue(orch.begin(cfg))

    def test_non_rightmost_conductor_raises_on_begin(self):
        ens = _ensemble("left", "middle", "right")
        orch = BigTextSpanOrchestrator(ens, "middle")
        cfg = SceneCfg(type="blank", name="b")
        with self.assertRaises(OrchestratorError) as cm:
            orch.begin(cfg)
        self.assertIn("rightmost", str(cm.exception))
        self.assertIn("'right'", str(cm.exception))
        self.assertIn("'middle'", str(cm.exception))

    def test_single_system_ensemble_treats_lone_system_as_rightmost(self):
        ens = _ensemble("only")
        orch = BigTextSpanOrchestrator(ens, "only")
        cfg = SceneCfg(type="blank", name="b")
        self.assertTrue(orch.begin(cfg))


class PublishSnapshotRoundTripTest(unittest.TestCase):
    def _bits(self, n_src_px: int) -> np.ndarray:
        return np.zeros((8, n_src_px), dtype=bool)

    def test_publish_and_snapshot(self):
        ens = _ensemble("left", "right")
        orch = BigTextSpanOrchestrator(ens, "right")
        orch.begin(SceneCfg(type="blank", name="b"))

        bits = self._bits(40)
        orch.publish_bits(bits=bits, color=7, rainbow=False, px_per_frame=2)
        orch.advance(123)

        snap = orch.snapshot()
        self.assertEqual(snap["abs_scroll_px"], 123)
        self.assertIs(snap["bits"], bits)
        self.assertEqual(snap["color"], 7)
        self.assertFalse(snap["rainbow"])
        self.assertEqual(snap["px_per_frame"], 2)
        self.assertEqual(snap["screen_w_px"], SCREEN_W_PX)

    def test_begin_after_publish_does_not_clobber_bits(self):
        # Tighter regression: full conductor sequence is
        # publish_bits → begin → snapshot, and the snapshot must
        # return the just-published bits (not None). Otherwise
        # followers wake up to an empty broadcast.
        ens = _ensemble("left", "right")
        orch = BigTextSpanOrchestrator(ens, "right")
        bits = self._bits(16)
        orch.publish_bits(bits=bits, color=7, rainbow=False, px_per_frame=1)
        orch.begin(SceneCfg(type="blank", name="b"))
        snap = orch.snapshot()
        self.assertIs(snap["bits"], bits)
        # abs_scroll_px is reset by begin (new broadcast starts at 0).
        self.assertEqual(snap["abs_scroll_px"], 0)

    def test_publish_bits_before_begin_does_not_raise(self):
        # The conductor's big_text overlay calls publish_bits BEFORE
        # begin() so followers see populated state the moment their
        # interrupt event fires. The state lock must therefore exist
        # outside the begin/end window — regression for the AttributeError
        # caught during phase-2 end-to-end verification.
        ens = _ensemble("left", "right")
        orch = BigTextSpanOrchestrator(ens, "right")
        orch.publish_bits(bits=self._bits(8), color=1, rainbow=False, px_per_frame=1)
        orch.advance(42)
        snap = orch.snapshot()
        self.assertEqual(snap["abs_scroll_px"], 42)
        self.assertIsNotNone(snap["bits"])

    def test_snapshot_before_publish_returns_none_bits(self):
        ens = _ensemble("left", "right")
        orch = BigTextSpanOrchestrator(ens, "right")
        orch.begin(SceneCfg(type="blank", name="b"))
        snap = orch.snapshot()
        self.assertIsNone(snap["bits"])

    def test_end_clears_bits(self):
        ens = _ensemble("left", "right")
        orch = BigTextSpanOrchestrator(ens, "right")
        orch.begin(SceneCfg(type="blank", name="b"))
        orch.publish_bits(bits=self._bits(8), color=1, rainbow=False, px_per_frame=1)
        orch.end()
        # Cannot snapshot after end (orchestrator is inactive) — the
        # bits cleanup is observable through end_threshold_px instead.
        # Re-begin to inspect.
        orch.begin(SceneCfg(type="blank", name="b"))
        self.assertIsNone(orch.snapshot()["bits"])


class WindowSlicingMathTest(unittest.TestCase):
    """`local_x_left_px(i, abs)` is the per-system slice math, pure
    function. The rightmost reduces to today's single-system formula."""

    def _orch(self, n: int = 3) -> BigTextSpanOrchestrator:
        names = ["s0", "s1", "s2", "s3", "s4"][:n]
        ens = _ensemble(*names)
        orch = BigTextSpanOrchestrator(ens, names[-1])
        orch.begin(SceneCfg(type="blank", name="b"))
        return orch

    def test_rightmost_matches_single_system_formula(self):
        # The rightmost (index N-1) should compute the same x_left_px
        # today's compose() does: SCREEN_W_PX - abs_scroll_px.
        orch = self._orch(3)
        for abs_px in (0, 50, 320, 700):
            with self.subTest(abs_px=abs_px):
                self.assertEqual(orch.local_x_left_px(2, abs_px), SCREEN_W_PX - abs_px)

    def test_abs_zero_message_just_off_right(self):
        # At abs_scroll_px = 0, message is just off the right of the
        # rightmost. Rightmost x_left_px == SCREEN_W_PX. Other systems
        # are further right (so > SCREEN_W_PX).
        orch = self._orch(3)
        self.assertEqual(orch.local_x_left_px(2, 0), SCREEN_W_PX)
        self.assertEqual(orch.local_x_left_px(1, 0), 2 * SCREEN_W_PX)
        self.assertEqual(orch.local_x_left_px(0, 0), 3 * SCREEN_W_PX)

    def test_message_fully_left_of_leftmost(self):
        # At abs_scroll_px = SCREEN_W_PX * N + n_src_px * 8, the
        # leftmost's x_left_px = -n_src_px*8 (fully scrolled past).
        orch = self._orch(3)
        n_src_px = 16
        orch.publish_bits(
            bits=np.zeros((8, n_src_px), dtype=bool), color=0, rainbow=False, px_per_frame=1
        )
        end = orch.end_threshold_px
        self.assertEqual(end, 3 * SCREEN_W_PX + n_src_px * 8)
        self.assertEqual(orch.local_x_left_px(0, end), -n_src_px * 8)

    def test_mid_scroll_message_spans_two_systems(self):
        # When the message is halfway across the wall (abs_scroll_px =
        # 1.5 * SCREEN_W_PX), the rightmost shows the second half and
        # the middle shows the first half emerging from the right.
        orch = self._orch(3)
        abs_px = SCREEN_W_PX + SCREEN_W_PX // 2  # 480
        # Rightmost (index 2): x_left_px = 320 - 480 = -160 → first
        # half is off-screen left, second half visible.
        self.assertEqual(orch.local_x_left_px(2, abs_px), -160)
        # Middle (index 1): x_left_px = 640 - 480 = 160 → first half
        # entering from the right edge.
        self.assertEqual(orch.local_x_left_px(1, abs_px), 160)


if __name__ == "__main__":
    unittest.main()
