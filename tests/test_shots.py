"""ShotTracker: where it calls a cut, and which frames each shot's fit sees."""

from __future__ import annotations

import unittest

import numpy as np

from c64cast.video.palette import ColorFitAccumulator
from c64cast.video.shots import ShotFit, ShotTracker

FRAME_S = 1 / 30


def _dark() -> np.ndarray:
    return np.full((120, 160, 3), 40, dtype=np.uint8)


def _bright(k: int) -> np.ndarray:
    """A ramp whose bottom rises 3 levels a frame: close enough to its
    neighbors to stay one shot, different enough that a fit over other frames
    comes out different."""
    ramp = np.linspace(100 + 3 * k, 230, 160).astype(np.uint8)
    return np.repeat(np.tile(ramp, (120, 1))[:, :, None], 3, axis=2)


def _observe_all(
    tracker: ShotTracker, frames: list[np.ndarray]
) -> list[tuple[int, float, ShotFit]]:
    found = []
    for i, frame in enumerate(frames):
        cut = tracker.observe(i * FRAME_S, frame)
        if cut is not None:
            found.append((i, *cut))
    return found


class ShotTrackerTest(unittest.TestCase):
    def _tracker(self, fit_frames: int = 4) -> ShotTracker:
        return ShotTracker(lambda frame: frame, fit_frames)

    def test_a_cut_is_reported_at_the_cut_frames_pts_two_frames_later(self):
        frames = [_dark() for _ in range(30)] + [_bright(k) for k in range(30)]
        found = _observe_all(self._tracker(), frames)
        self.assertEqual([(i, pts) for i, pts, _ in found], [(32, 30 * FRAME_S)])

    def test_a_shots_fit_covers_its_first_frames_and_no_others(self):
        frames = [_dark() for _ in range(30)] + [_bright(k) for k in range(30)]
        [(_, _, shot)] = _observe_all(self._tracker(fit_frames=4), frames)
        expected = ColorFitAccumulator(strength=1.0)
        for k in range(4):
            expected.add(_bright(k))
        self.assertEqual(shot.result(), expected.result())

    def test_one_unbroken_shot_reports_no_cut(self):
        self.assertEqual(_observe_all(self._tracker(), [_bright(k) for k in range(40)]), [])

    def test_the_tracker_fits_the_prepared_frame(self):
        frames = [_dark() for _ in range(30)] + [_bright(k) for k in range(30)]
        tracker = ShotTracker(lambda frame: frame[:, 80:], fit_frames=4)
        [(_, _, shot)] = _observe_all(tracker, frames)
        expected = ColorFitAccumulator(strength=1.0)
        for k in range(4):
            expected.add(_bright(k)[:, 80:])
        self.assertEqual(shot.result(), expected.result())


if __name__ == "__main__":
    unittest.main()
