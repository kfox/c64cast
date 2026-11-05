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

    def test_a_seek_back_soon_after_a_cut_starts_a_shot_at_the_landing_frame(self):
        tracker = self._tracker(fit_frames=4)
        _observe_all(tracker, [_dark() for _ in range(30)] + [_bright(k) for k in range(6)])
        tracker.restart()
        landing = [tracker.observe(k * FRAME_S, _dark()) for k in range(5)]
        self.assertEqual([c[0] for c in landing if c is not None], [0.0])
        expected = ColorFitAccumulator(strength=1.0)
        for _ in range(4):
            expected.add(_dark())
        assert landing[0] is not None
        self.assertEqual(landing[0][1].result(), expected.result())

    def test_a_seek_landing_on_the_picture_it_left_starts_no_shot(self):
        tracker = self._tracker()
        _observe_all(tracker, [_dark() for _ in range(30)])
        tracker.restart()
        self.assertEqual([tracker.observe(k * FRAME_S, _dark()) for k in range(5)], [None] * 5)

    def test_a_cut_soon_after_a_seek_is_found(self):
        tracker = self._tracker()
        _observe_all(tracker, [_dark() for _ in range(30)] + [_bright(k) for k in range(6)])
        tracker.restart()
        after = _observe_all(tracker, [_dark() for _ in range(4)] + [_bright(k) for k in range(10)])
        self.assertEqual([pts for _, pts, _ in after], [0.0, 4 * FRAME_S])

    def test_no_cut_after_a_seek_carries_a_stamp_from_before_it(self):
        tracker = self._tracker()
        frames = [_dark() for _ in range(30)] + [_bright(k) for k in range(30)]
        for i, frame in enumerate(frames[:31]):
            tracker.observe(100 + i * FRAME_S, frame)
        tracker.restart()
        stamps = [c[0] for i, f in enumerate(frames[31:]) if (c := tracker.observe(i * FRAME_S, f))]
        self.assertTrue(all(pts < 100 for pts in stamps), stamps)


if __name__ == "__main__":
    unittest.main()
