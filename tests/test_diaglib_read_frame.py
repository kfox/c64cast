"""The diag tools' frame read: a capture that returns nothing for a moment is
retried for a bounded window, and one that never answers fails naming why."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _DIAGS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_tools() -> tuple[ModuleType, ModuleType]:
    """scripts/diags/ is not a package; load _diaglib and hdmi_capture by path
    without leaving either, or _diaglib's sys.path insert, behind."""
    with patch.object(sys, "path", [str(_DIAGS), *sys.path]), patch.dict(sys.modules):
        return _load("_diaglib"), _load("hdmi_capture")


_diaglib, hdmi_capture = _load_tools()

FRAME = object()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeCapture:
    """A cv2.VideoCapture stand-in whose reads fail ``fail_first`` times (each
    costing ``fail_cost_s`` of fake time, as a Cam Link read does), then
    succeed until read number ``fail_at`` (1-based), which fails."""

    def __init__(
        self,
        clock: FakeClock,
        fail_first: float,
        *,
        fail_cost_s: float = 1.0,
        fail_at: int | None = None,
    ) -> None:
        self.clock = clock
        self.fail_first = fail_first
        self.fail_cost_s = fail_cost_s
        self.fail_at = fail_at
        self.reads = 0
        self.released = False

    def read(self):
        self.reads += 1
        if self.reads <= self.fail_first or self.reads == self.fail_at:
            self.clock.now += self.fail_cost_s
            return False, None
        return True, FRAME

    def set(self, prop, value) -> bool:
        return True

    def release(self) -> None:
        self.released = True


class _FakeTimeTest(unittest.TestCase):
    """Runs _diaglib on a fake clock whose sleep returns at once."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        fake_time = SimpleNamespace(monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        patcher = patch.object(_diaglib, "time", fake_time)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReadFrameTest(_FakeTimeTest):
    def test_returns_the_frame_after_transient_failures(self):
        cap = FakeCapture(self.clock, fail_first=3)
        self.assertIs(_diaglib.read_frame(cap, "Cam Link"), FRAME)
        self.assertEqual(cap.reads, 4)

    def test_never_answering_fails_after_the_window_naming_the_causes(self):
        cap = FakeCapture(self.clock, fail_first=float("inf"))
        with self.assertRaises(_diaglib.NoFrameError) as caught:
            _diaglib.read_frame(cap, "Cam Link")
        message = str(caught.exception)
        self.assertIn("'Cam Link'", message)
        self.assertIn("renegotiating", message)
        self.assertIn("no signal", message)
        self.assertIn("another program holds the device", message)
        self.assertIn("c64cast --list-devices", message)
        # Gave the device the whole window, and stopped within one read of it.
        self.assertGreaterEqual(self.clock.now, _diaglib.NO_FRAME_RETRY_S)
        self.assertLess(
            self.clock.now, _diaglib.NO_FRAME_RETRY_S + cap.fail_cost_s + _diaglib.NO_FRAME_POLL_S
        )

    def test_a_read_that_fails_instantly_is_paced_not_spun(self):
        cap = FakeCapture(self.clock, fail_first=float("inf"), fail_cost_s=0.0)
        with self.assertRaises(_diaglib.NoFrameError):
            _diaglib.read_frame(cap, 0, timeout_s=1.0)
        self.assertLessEqual(cap.reads, round(1.0 / _diaglib.NO_FRAME_POLL_S) + 1)

    def test_grab_retries_then_exits_with_the_message(self):
        cap = FakeCapture(self.clock, fail_first=float("inf"))
        with patch.object(_diaglib, "open_capture", return_value=cap):
            with self.assertRaises(SystemExit) as caught:
                hdmi_capture.grab("Cam Link", warmup=2)
        self.assertIn("renegotiating", str(caught.exception))
        self.assertTrue(cap.released)

    def test_grab_returns_a_frame_that_arrives_inside_the_window(self):
        cap = FakeCapture(self.clock, fail_first=4)
        with patch.object(_diaglib, "open_capture", return_value=cap):
            self.assertIs(hdmi_capture.grab("Cam Link", warmup=2), FRAME)


class BurstTest(_FakeTimeTest):
    def _burst(self, cap: FakeCapture, count: int):
        with patch.object(_diaglib, "open_capture", return_value=cap):
            return hdmi_capture.burst("Cam Link", count, size=(1280, 720), fps=60, warmup=2)

    def test_the_first_frame_waits_for_the_link(self):
        cap = FakeCapture(self.clock, fail_first=4)
        frames, _ = self._burst(cap, 3)
        self.assertEqual(frames, [FRAME] * 3)

    def test_a_mid_burst_failure_is_not_retried(self):
        # warm-up is reads 1-2, the first kept frame read 3; read 5 fails.
        cap = FakeCapture(self.clock, fail_first=0, fail_at=5)
        with self.assertRaises(SystemExit) as caught:
            self._burst(cap, 4)
        message = str(caught.exception)
        self.assertIn("mid-burst, after 2 of 4 frames", message)
        self.assertIn("renegotiating", message)
        self.assertEqual(cap.reads, 5)
        self.assertTrue(cap.released)


if __name__ == "__main__":
    unittest.main()
