"""`[color].hardware_palette_shots`: the split blind push in c64cast.hw.uci,
the stretch tracker in c64cast.video.palette_shots, and the pusher in
c64cast.hw.hardware_palette that times one to the other."""

from __future__ import annotations

import threading
import time
import unittest
from typing import cast
from unittest import mock

import numpy as np
from _fakes import mock_backend
from test_hardware_palette import MACHINE, PaletteSwapTestCase, _FakeApi, _warm_image
from test_uci import PALETTE_BYTES, PALETTE_RGB, _FakeUltimate

from c64cast.hw import hardware_palette as hp
from c64cast.hw import uci
from c64cast.hw.api import Ultimate64API
from c64cast.video import palette
from c64cast.video import palette_shots as ps

FPS = 25.0


class _Stop:
    def __init__(self, after: int) -> None:
        self.calls = 0
        self.after = after

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.after


class BlindPushTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(uci.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_puts_the_command_in_the_fifo_without_pushing_or_reading(self):
        device = _FakeUltimate(reply=b"")
        self.assertTrue(uci.load_palette(device, PALETTE_RGB, stop=lambda: False))
        self.assertEqual(device.commands, [0x04, 0x52, *PALETTE_BYTES])
        self.assertEqual(device.control_writes, [])
        self.assertEqual(device._reads, 0)

    def test_load_trickles_five_bytes_a_flush(self):
        device = _FakeUltimate(reply=b"")
        uci.load_palette(device, PALETTE_RGB, stop=lambda: False)
        self.assertEqual(device.flushes, 10)
        self.assertEqual(self.sleep.call_count, 10)
        self.sleep.assert_called_with(uci.LOAD_GAP_S)

    def test_a_stopped_load_says_so_and_writes_no_more(self):
        device = _FakeUltimate(reply=b"")
        self.assertFalse(uci.load_palette(device, PALETTE_RGB, stop=_Stop(after=3)))
        self.assertEqual(len(device.commands), 3 * uci.LOAD_CHUNK_BYTES)

    def test_go_then_ack_runs_the_command_and_frees_the_interface_unread(self):
        device = _FakeUltimate(reply=b"")
        uci.load_palette(device, PALETTE_RGB, stop=lambda: False)
        flushes = device.flushes
        uci.go(device)
        self.assertEqual(device.flushes, flushes)
        device.state = 0x20  # the firmware has finished the command
        uci.ack(device)
        self.assertEqual(device.control_writes, [uci.CONTROL_PUSH_CMD, uci.CONTROL_NEXT_DATA])
        self.assertTrue(device.released)
        self.assertEqual(device._reads, 0)

    def test_refuses_a_palette_that_is_not_sixteen_triples(self):
        with self.assertRaises(ValueError):
            uci.load_palette(_FakeUltimate(), PALETTE_RGB[:15], stop=lambda: False)


def _solid(bgr, noise: int = 0, seed: int = 0) -> np.ndarray:
    img = np.empty((60, 80, 3), np.int16)
    img[:] = bgr
    if noise:
        img += np.random.default_rng(seed).integers(-noise, noise + 1, img.shape, dtype=np.int16)
    return np.clip(img, 0, 255).astype(np.uint8)


RED = (40, 40, 200)
DIM_RED = tuple(round(v * 0.3) for v in RED)
BLUE = (200, 60, 30)


class _Recorder:
    """Collects the stretches handed over, fitting each one as the pusher
    would, so the tracker can measure drift against it."""

    def __init__(self) -> None:
        self.ready: list[ps.PaletteShot] = []

    def __call__(self, shot: ps.PaletteShot) -> None:
        table = palette.derive_hardware_palette(shot.frames, MACHINE)
        assert table is not None
        base = float(np.mean([palette.hardware_palette_error(f, table) for f in shot.frames]))
        shot.fitted(table, base)
        self.ready.append(shot)


def _track(frames) -> tuple[list[float], _Recorder, ps.PaletteShotTracker]:
    rec = _Recorder()
    tracker = ps.PaletteShotTracker(lambda f: f, lambda f: f, rec)
    starts = []
    for i, f in enumerate(frames):
        found = tracker.observe(i / FPS, f)
        if found is not None:
            starts.append(found[0])
    return starts, rec, tracker


def _frames(*parts) -> list[np.ndarray]:
    out = []
    for color, seconds in parts:
        out += [_solid(color, noise=6, seed=len(out) + i) for i in range(int(seconds * FPS))]
    return out


class PaletteShotTrackerTest(unittest.TestCase):
    def test_the_first_frame_and_each_cut_start_a_stretch(self):
        starts, rec, _ = _track(_frames((RED, 3), (BLUE, 3), (RED, 3)))
        self.assertEqual(starts, [0.0, 3.0, 6.0])
        self.assertTrue(rec.ready[0].starts_over)
        self.assertFalse(rec.ready[1].starts_over)

    def test_a_stretch_is_fitted_to_its_first_two_seconds(self):
        _, rec, _ = _track(_frames((RED, 3), (BLUE, 3)))
        self.assertEqual(len(rec.ready[0].frames), int(ps.FIT_S * FPS))

    def test_a_shot_shorter_than_a_load_keeps_the_colors_before(self):
        starts, _, _ = _track(_frames((RED, 3), (BLUE, 1), (RED, 3)))
        self.assertEqual(starts, [0.0, 3.0])

    def test_a_short_shot_is_handed_over_with_the_frames_it_has(self):
        _, rec, tracker = _track(_frames((RED, 1.5), (BLUE, 3)))
        self.assertEqual(len(rec.ready[0].frames), int(1.5 * FPS))

    def test_a_fade_the_cut_detector_misses_starts_a_stretch_on_drift(self):
        fade = int(1.0 * FPS)
        frames = _frames((RED, 3))
        for k in range(fade):
            t = (k + 1) / fade
            dim = tuple(round(v * (1 - 0.7 * t)) for v in RED)
            frames.append(_solid(dim, noise=6, seed=1000 + k))
        frames += _frames((DIM_RED, 3))
        starts, _, _ = _track(frames)
        self.assertEqual(len(starts), 2, starts)
        self.assertGreaterEqual(starts[1], 3.0)
        self.assertLess(starts[1], 4.0)

    def test_without_the_drift_check_that_fade_is_one_stretch(self):
        frames = _frames((RED, 3))
        for k in range(int(FPS)):
            dim = tuple(round(v * (1 - 0.7 * (k + 1) / FPS)) for v in RED)
            frames.append(_solid(dim, noise=6, seed=1000 + k))
        frames += _frames((DIM_RED, 3))
        with mock.patch.object(ps, "DRIFT_RATIO", 1e9):
            starts, _, _ = _track(frames)
        self.assertEqual(starts, [0.0])

    def test_steady_frames_never_drift(self):
        starts, _, _ = _track(_frames((RED, 8)))
        self.assertEqual(starts, [0.0])

    def test_a_restart_starts_over_on_the_next_frame(self):
        _, rec, tracker = _track(_frames((RED, 3)))
        tracker.restart(None)
        found = tracker.observe(10.0, _solid(RED))
        assert found is not None
        self.assertEqual(found[0], 10.0)
        self.assertTrue(found[1].starts_over)


class _FakeControl:
    """The HardwarePalette surface ShotPusher drives, recording the order."""

    def __init__(self) -> None:
        self.machine_palette = MACHINE.copy()
        self.calls: list[str] = []
        self.adopted: list[np.ndarray] = []
        self.load_ok = True

    def load(self, table, stop) -> bool:
        self.calls.append("load")
        return self.load_ok and not stop()

    def adopt(self, table) -> bool:
        self.calls.append("adopt")
        self.adopted.append(table)
        return True

    def go(self) -> None:
        self.calls.append("go")

    def ack(self) -> None:
        self.calls.append("ack")

    def abort(self) -> None:
        self.calls.append("abort")


def _shot(color, pts: float, **kw) -> ps.PaletteShot:
    shot = ps.PaletteShot(pts, **kw)
    shot.frames = [_solid(color, noise=6, seed=s) for s in range(5)]
    return shot


def _until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


class ShotPusherTest(unittest.TestCase):
    def setUp(self):
        self.control = _FakeControl()
        self.pusher = hp.ShotPusher(cast(hp.HardwarePalette, self.control))
        self.pusher.start()
        self.addCleanup(self.pusher.stop)

    def test_loads_ahead_and_pushes_after_the_first_frame_renders(self):
        shot = _shot(RED, 0.0)
        self.pusher.ready(shot)
        self.assertTrue(self.pusher.wait_loaded(5.0))
        self.assertEqual(self.control.calls, ["load"])
        self.assertTrue(self.pusher.before_render(shot))
        self.assertEqual(self.control.calls, ["load", "adopt"])
        fit = shot.fit
        assert fit is not None
        np.testing.assert_array_equal(self.control.adopted[0], fit[0])
        self.pusher.after_render()
        self.assertTrue(_until(lambda: self.control.calls == ["load", "adopt", "go", "ack"]))

    def test_the_next_table_loads_only_after_the_ack(self):
        first, second = _shot(RED, 0.0), _shot(BLUE, 3.0)
        self.pusher.ready(first)
        self.pusher.ready(second)
        self.assertTrue(self.pusher.wait_loaded(5.0))
        time.sleep(0.05)
        self.assertEqual(self.control.calls, ["load"])
        self.pusher.before_render(first)
        self.pusher.after_render()
        self.assertTrue(_until(lambda: self.control.calls.count("load") == 2))
        self.assertEqual(self.control.calls, ["load", "adopt", "go", "ack", "load"])

    def test_a_table_loaded_after_its_frame_is_pushed_on_a_later_one(self):
        shot = _shot(RED, 0.0)
        self.assertFalse(self.pusher.before_render(shot))
        self.pusher.ready(shot)
        self.assertTrue(self.pusher.wait_loaded(5.0))
        self.assertTrue(self.pusher.before_render(None))

    def test_other_frames_carry_no_push(self):
        shot = _shot(RED, 0.0)
        self.pusher.ready(shot)
        self.pusher.wait_loaded(5.0)
        self.assertFalse(self.pusher.before_render(None))
        self.assertTrue(self.pusher.before_render(shot))
        self.pusher.after_render()
        self.assertFalse(self.pusher.before_render(None))

    def test_a_loaded_table_whose_stretch_has_passed_is_dropped(self):
        first, second = _shot(RED, 0.0), _shot(BLUE, 3.0)
        self.pusher.ready(first)
        self.pusher.ready(second)
        self.pusher.wait_loaded(5.0)
        self.assertFalse(self.pusher.before_render(second))
        self.assertTrue(_until(lambda: self.control.calls.count("load") == 2))
        self.assertEqual(self.control.calls, ["load", "abort", "load"])
        self.assertTrue(self.pusher.before_render(None))
        fit = second.fit
        assert fit is not None
        np.testing.assert_array_equal(self.control.adopted[0], fit[0])

    def test_a_seek_drops_a_table_loading_from_before_it_ahead_of_any_frame(self):
        loading, gate = threading.Event(), threading.Event()
        load = self.control.load

        def held_load(table, stop):
            if not loading.is_set():
                loading.set()
                gate.wait(5.0)
            return load(table, stop)

        self.control.load = held_load  # type: ignore[method-assign]
        self.pusher.ready(_shot(RED, 0.0))
        self.assertTrue(loading.wait(5.0))
        after = _shot(BLUE, 20.0, starts_over=True)
        self.pusher.ready(after)
        gate.set()
        self.assertTrue(_until(lambda: self.control.calls == ["load", "abort", "load"]))
        self.assertTrue(self.pusher.before_render(after))
        fit = after.fit
        assert fit is not None
        np.testing.assert_array_equal(self.control.adopted[0], fit[0])

    def test_a_failed_load_is_aborted_and_not_pushed(self):
        self.control.load_ok = False
        shot = _shot(RED, 0.0)
        self.pusher.ready(shot)
        self.assertTrue(_until(lambda: "abort" in self.control.calls))
        self.assertFalse(self.pusher.before_render(shot))

    def test_no_table_loads_between_the_push_and_its_ack(self):
        later = _shot(BLUE, 3.0)

        def go_while_the_next_stretch_comes_in():
            self.control.calls.append("go")
            self.pusher.ready(later)
            _until(lambda: self.control.calls.count("load") == 2, timeout_s=0.5)

        self.control.go = go_while_the_next_stretch_comes_in  # type: ignore[method-assign]
        shot = _shot(RED, 0.0)
        self.pusher.ready(shot)
        self.assertTrue(self.pusher.wait_loaded(5.0))
        self.assertTrue(self.pusher.before_render(shot))
        self.pusher.after_render()
        self.assertTrue(_until(lambda: self.control.calls.count("load") == 2))
        self.assertEqual(self.control.calls, ["load", "adopt", "go", "ack", "load"])

    def test_stop_drops_a_loaded_table(self):
        self.pusher.ready(_shot(RED, 0.0))
        self.pusher.wait_loaded(5.0)
        self.pusher.stop()
        self.assertEqual(self.control.calls, ["load", "abort"])

    def test_stop_with_nothing_outstanding_writes_nothing(self):
        self.pusher.stop()
        self.assertEqual(self.control.calls, [])


class BlindControlTest(PaletteSwapTestCase):
    def setUp(self):
        super().setUp()
        palette.set_host_palette(palette.C64_WIKI_PALETTE_BGR, name="c64-wiki")
        self.api = _FakeApi()
        self.control = hp.HardwarePalette(cast(Ultimate64API, self.api), MACHINE)
        self.table = MACHINE.copy()
        self.table[8] = (30, 95, 175)

    def test_adopt_points_the_quantizer_at_the_table(self):
        self.assertTrue(self.control.adopt(self.table))
        np.testing.assert_array_equal(palette.C64_PALETTE_BGR, self.table)

    def test_a_link_failure_turns_pushing_off(self):
        with (
            mock.patch.object(hp.uci, "load_palette", side_effect=OSError("link down")),
            self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"),
        ):
            self.assertFalse(self.control.load(self.table, lambda: False))
        self.assertFalse(self.control.adopt(self.table))
        np.testing.assert_array_equal(palette.C64_PALETTE_BGR, palette.C64_WIKI_PALETTE_BGR)

    def test_restore_puts_the_machines_table_back_after_an_adopted_one(self):
        self.control.adopt(self.table)
        with mock.patch.object(hp.uci, "set_palette_rgb", return_value=True) as push:
            with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
                self.control.restore()
        push.assert_called_once()


class FrameRenderPushTest(unittest.TestCase):
    """process_frame hands the stretch whose first frame it shows to the
    pusher before the render, and owes it `after_render` once the frame's
    writes are out, even when they failed."""

    def setUp(self):
        from test_video import _make_video_scene_stub, _StubSource

        self.shot = ps.PaletteShot(0.0)
        source = _StubSource()
        source.shots = [self.shot]  # type: ignore[list-item]
        self.scene = _make_video_scene_stub(source)
        cast(mock.MagicMock, self.scene.api).delivery_epoch = 0
        self.order: list[str] = []
        pusher = mock.Mock(spec=hp.ShotPusher)
        pusher.before_render.side_effect = lambda shot: self.order.append("before") or True
        pusher.after_render.side_effect = lambda: self.order.append("after")
        self.pusher = pusher
        self.scene._shot_pusher = pusher

    def _show(self, render) -> None:
        from c64cast.scenes import scenes

        with (
            mock.patch.object(scenes, "_render_with_overlays", side_effect=render),
            mock.patch.object(scenes, "crop_to_aspect", side_effect=lambda img, _aspect: img),
        ):
            self.scene.process_frame(0.0)

    def test_the_push_follows_the_frames_writes(self):
        self._show(lambda *a: self.order.append("render"))
        self.pusher.before_render.assert_called_once_with(self.shot)
        self.assertEqual(self.order, ["before", "render", "after"])

    def test_a_failed_render_still_sends_the_push(self):
        from c64cast.hw.backend import LinkError

        def fail(*a):
            self.order.append("render")
            raise LinkError("link down")

        with self.assertRaises(LinkError):
            self._show(fail)
        self.assertEqual(self.order, ["before", "render", "after"])


class VideoShotWiringTest(unittest.TestCase):
    def setUp(self):
        from c64cast.app.config import ColorCfg
        from c64cast.scenes.scenes import VideoScene

        self.control = mock.Mock(spec=hp.HardwarePalette)
        self.control.machine_palette = MACHINE.copy()
        api = mock_backend()
        api.hardware_palette = self.control
        self.mode = mock.MagicMock()
        self.mode.quantizer_input.side_effect = lambda img: img
        self.scene = VideoScene(
            api=api,
            audio=None,
            display_mode=self.mode,
            file="https://stub.invalid/clip.mp4",
            color=ColorCfg(hardware_palette="source"),
            setup_progress=False,
        )
        patches = [
            mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            mock.patch("c64cast.scenes.scenes.AVFileSource"),
            mock.patch("c64cast.scenes.scenes.prescan_source_color", side_effect=self._prescan),
            mock.patch("c64cast.scenes.scenes.FIRST_SHOT_PALETTE_WAIT_S", 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _prescan(self, path, **kw):
        kw["frames"].add(_warm_image())
        return None, None

    def test_setup_tracks_shots_and_teardown_stops_the_pusher(self):
        with self.assertLogs("c64cast.scenes.scenes", level="INFO"):
            self.scene.setup()
        source = self.scene.source
        assert source is not None
        self.assertIsInstance(source.shot_tracker, ps.PaletteShotTracker)
        pusher = self.scene._shot_pusher
        assert pusher is not None
        self.scene.teardown()
        self.assertIsNone(self.scene._shot_pusher)
        self.assertFalse(pusher._thread.is_alive())

    def test_off_keeps_one_palette_per_video(self):
        self.scene._color.hardware_palette_shots = False
        self.scene.setup()
        self.assertIsNone(self.scene._shot_pusher)
        self.scene.teardown()


if __name__ == "__main__":
    unittest.main()
