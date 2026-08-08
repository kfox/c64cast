"""PreviewWindow: the cv2 HighGUI mirror window + its main-thread pump.

cv2 is a hard dep, so the window needs no optional extra — but it does need a
desktop session, which CI hasn't got. Every test here patches
`c64cast.video.preview.cv2`, so nothing opens a real window.

The pump contract these lock down (see docs/caveats.md → "Preview window
fidelity + limits"): drawing is rate-limited to `fps` while `waitKey` runs on
*every* pump (it's HighGUI's event loop), and no failure mode — headless
opencv, a render blowup, the user closing the window — may propagate, because
`pump()` runs on the main thread and would take the session down with it.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from c64cast.video import preview as preview_mod
from c64cast.video.preview import PreviewWindow


def _fake_cv2() -> MagicMock:
    """A cv2 stand-in with real numbers where the code compares or indexes.
    A bare MagicMock isn't enough: `getWindowProperty(...) < 1` would raise
    TypeError on a mock return, masking the behavior under test."""
    m = MagicMock()
    m.WINDOW_AUTOSIZE = 1
    m.WND_PROP_VISIBLE = 4
    m.INTER_NEAREST = 0
    # 1.0 == window still visible. Tests that care flip this.
    m.getWindowProperty.return_value = 1.0
    m.resize.side_effect = lambda img, dsize, interpolation=None: np.zeros(
        (dsize[1], dsize[0], 3), dtype=np.uint8
    )
    return m


def _fake_fb() -> MagicMock:
    fb = MagicMock()
    fb.render.return_value = np.zeros((200, 320, 3), dtype=np.uint8)
    return fb


def _fake_clock(*ticks: float) -> SimpleNamespace:
    """A `time` stand-in for the preview module, handing out `ticks` in order.

    Bind this over `preview_mod.time` — the *name* inside preview — and never
    over `time.monotonic` itself. Patching an attribute of the stdlib module
    rebinds it for the entire process, threads included, and the suite leaves
    feature workers running that call `time.monotonic()` hundreds of times a
    second. One of those landing between two `pump()` calls empties the tick
    list early; the `StopIteration` is then swallowed by `pump()`'s catch-all,
    which disables the window and leaves the draw count one short. That only
    happens when every module shares one process — `make coverage`, not
    `make test`, which forks per module."""
    return SimpleNamespace(monotonic=iter(ticks).__next__)


class PreviewWindowOpenTest(unittest.TestCase):
    def test_open_creates_autosize_window(self):
        # AUTOSIZE (not NORMAL) is deliberate: we upscale by an integer factor
        # ourselves so HighGUI never interpolates C64 pixels.
        cv2 = _fake_cv2()
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), title="t")
            win.open()
        cv2.namedWindow.assert_called_once_with("t", cv2.WINDOW_AUTOSIZE)
        self.assertTrue(win.is_open)

    def test_open_is_idempotent(self):
        cv2 = _fake_cv2()
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), title="t")
            win.open()
            win.open()
        self.assertEqual(cv2.namedWindow.call_count, 1)

    def test_open_failure_disables_window_without_raising(self):
        # The headless-opencv case: no GUI support, so namedWindow throws.
        # The session must survive it.
        cv2 = _fake_cv2()
        cv2.namedWindow.side_effect = RuntimeError("no GUI support")
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), title="t")
            win.open()  # must not raise
        self.assertFalse(win.is_open)

    def test_is_open_false_before_open(self):
        with patch.object(preview_mod, "cv2", _fake_cv2()):
            self.assertFalse(PreviewWindow(_fake_fb()).is_open)


class PreviewWindowPumpTest(unittest.TestCase):
    def test_pump_before_open_is_noop(self):
        cv2 = _fake_cv2()
        fb = _fake_fb()
        with patch.object(preview_mod, "cv2", cv2):
            PreviewWindow(fb).pump()
        fb.render.assert_not_called()
        cv2.imshow.assert_not_called()
        cv2.waitKey.assert_not_called()

    def test_pump_draws_scaled_frame(self):
        cv2 = _fake_cv2()
        fb = _fake_fb()
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(fb, scale=3, title="t")
            win.open()
            win.pump()
        fb.render.assert_called_once()
        cv2.resize.assert_called_once()
        self.assertEqual(cv2.resize.call_args.args[1], (960, 600))
        cv2.imshow.assert_called_once()
        self.assertEqual(cv2.imshow.call_args.args[0], "t")
        self.assertEqual(cv2.imshow.call_args.args[1].shape, (600, 960, 3))

    def test_scale_one_skips_the_resize(self):
        cv2 = _fake_cv2()
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), scale=1)
            win.open()
            win.pump()
        cv2.resize.assert_not_called()
        cv2.imshow.assert_called_once()

    def test_pump_rate_limits_draws_but_always_pumps_events(self):
        # Two pumps inside one frame period: one draw, two waitKeys. Skipping
        # waitKey would leave the window unpainted and OS-unresponsive.
        cv2 = _fake_cv2()
        fb = _fake_fb()
        with (
            patch.object(preview_mod, "cv2", cv2),
            patch.object(preview_mod, "time", _fake_clock(100.0, 100.001)),
        ):
            win = PreviewWindow(fb, fps=30)
            win.open()
            win.pump()
            win.pump()
        self.assertEqual(fb.render.call_count, 1)
        self.assertEqual(cv2.imshow.call_count, 1)
        self.assertEqual(cv2.waitKey.call_count, 2)

    def test_pump_redraws_once_the_frame_deadline_passes(self):
        cv2 = _fake_cv2()
        fb = _fake_fb()
        with (
            patch.object(preview_mod, "cv2", cv2),
            # 1/30 s apart, so the second pump is due.
            patch.object(preview_mod, "time", _fake_clock(100.0, 100.5)),
        ):
            win = PreviewWindow(fb, fps=30)
            win.open()
            win.pump()
            win.pump()
        self.assertEqual(fb.render.call_count, 2)
        self.assertEqual(cv2.imshow.call_count, 2)

    def test_pump_survives_a_render_failure_and_disables_itself(self):
        # pump() is on the main thread; an escaping exception would kill the run.
        cv2 = _fake_cv2()
        fb = _fake_fb()
        fb.render.side_effect = ValueError("bad frame")
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(fb)
            win.open()
            with self.assertLogs("c64cast.video.preview", level="ERROR"):
                win.pump()  # must not raise
        self.assertFalse(win.is_open)
        # Disabled means disabled: no further work on later pumps.
        with patch.object(preview_mod, "cv2", cv2):
            win.pump()
        self.assertEqual(fb.render.call_count, 1)

    def test_pump_closes_when_user_dismisses_the_window(self):
        cv2 = _fake_cv2()
        cv2.getWindowProperty.return_value = 0.0  # no longer visible
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), title="t")
            win.open()
            win.pump()
        self.assertFalse(win.is_open)
        cv2.destroyWindow.assert_called_once_with("t")

    def test_visibility_probe_failure_counts_as_closed(self):
        cv2 = _fake_cv2()
        cv2.getWindowProperty.side_effect = RuntimeError("no such window")
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb())
            win.open()
            win.pump()
        self.assertFalse(win.is_open)


class PreviewWindowCloseTest(unittest.TestCase):
    def test_close_destroys_the_window(self):
        cv2 = _fake_cv2()
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), title="t")
            win.open()
            win.close()
        cv2.destroyWindow.assert_called_once_with("t")
        self.assertFalse(win.is_open)

    def test_close_before_open_is_a_noop(self):
        cv2 = _fake_cv2()
        with patch.object(preview_mod, "cv2", cv2):
            PreviewWindow(_fake_fb()).close()
        cv2.destroyWindow.assert_not_called()

    def test_close_is_idempotent(self):
        cv2 = _fake_cv2()
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb(), title="t")
            win.open()
            win.close()
            win.close()
        self.assertEqual(cv2.destroyWindow.call_count, 1)

    def test_close_survives_a_failing_destroy(self):
        cv2 = _fake_cv2()
        cv2.destroyWindow.side_effect = RuntimeError("already gone")
        with patch.object(preview_mod, "cv2", cv2):
            win = PreviewWindow(_fake_fb())
            win.open()
            win.close()  # must not raise
        self.assertFalse(win.is_open)


class PumpPreviewsUntilDoneTest(unittest.TestCase):
    """cli._pump_previews_until_done: the main-thread driver. It exists because
    HighGUI windows can only live on the main thread while every playlist runs
    on a worker (see docs/caveats.md)."""

    @staticmethod
    def _thread(alive: list[bool]) -> MagicMock:
        t = MagicMock()
        t.is_alive.side_effect = alive
        return t

    def test_opens_pumps_and_joins(self):
        from c64cast.cli import _pump_previews_until_done

        win = MagicMock()
        win.is_open = True
        # Alive for two loop iterations, then done.
        t = self._thread([True, True, False])
        _pump_previews_until_done([t], [win])
        win.open.assert_called_once()
        self.assertEqual(win.pump.call_count, 2)
        t.join.assert_called_once_with()

    def test_stops_pumping_once_every_window_is_closed(self):
        # Closing the window is not a stop signal: we bail out of the pump loop
        # and fall through to a plain blocking join so playback carries on.
        from c64cast.cli import _pump_previews_until_done

        win = MagicMock()
        win.is_open = False  # user closed it before the first check
        t = self._thread([True])
        _pump_previews_until_done([t], [win])
        self.assertEqual(win.pump.call_count, 1)
        t.join.assert_called_once_with()

    def test_pumps_every_window_in_an_ensemble(self):
        from c64cast.cli import _pump_previews_until_done

        wins = [MagicMock(), MagicMock()]
        for w in wins:
            w.is_open = True
        t = self._thread([True, False])
        _pump_previews_until_done([t], wins)
        for w in wins:
            w.open.assert_called_once()
            self.assertEqual(w.pump.call_count, 1)


if __name__ == "__main__":
    unittest.main()
