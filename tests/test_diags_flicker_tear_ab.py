"""flicker_tear_ab writes configs c64cast accepts, with blending where it means it."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from _fakes import MachineSettingsIsolation
from test_diags_nmi_latch import _load

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory

_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


class PhaseConfigTest(unittest.TestCase):
    def _resolved_tolerance(self, *, flicker: bool) -> str:
        tool = _load("flicker_tear_ab")
        out = Path(tempfile.mkdtemp())
        cfg_path = out / "phase.toml"
        tool.write_config(cfg_path, out / "ab.mkv", flicker=flicker)
        cfg = cfgmod.load(str(cfg_path))
        for validate in scene_factory.PER_SYSTEM_VALIDATORS:
            validate(cfg)
        (scene,) = cfg.scenes
        assert scene.display is not None
        return scene_factory.resolve_flicker_tolerance(
            cfgmod.scene_color(cfg, scene).flicker_tolerance, scene.display
        )

    def test_the_flicker_phase_arms_blending(self) -> None:
        self.assertNotEqual(self._resolved_tolerance(flicker=True), "off")

    def test_the_plain_phase_leaves_it_off(self) -> None:
        self.assertEqual(self._resolved_tolerance(flicker=False), "off")

    def test_a_windows_clip_path_is_written_as_itself(self) -> None:
        tool = _load("flicker_tear_ab")
        cfg_path = Path(tempfile.mkdtemp()) / "phase.toml"
        clip = r"C:\Users\runner\AppData\Local\Temp\ab.mkv"
        tool.write_config(cfg_path, clip, flicker=True)
        (scene,) = cfgmod.load(str(cfg_path)).scenes
        self.assertEqual(scene.file, clip)

    def test_a_clip_path_outside_the_bmp_or_with_del_is_written_as_itself(self) -> None:
        tool = _load("flicker_tear_ab")
        for clip in ("/clips/\U0001f3b5 track.mkv", "/clips/a\x7fb.mkv"):
            with self.subTest(clip=clip):
                cfg_path = Path(tempfile.mkdtemp()) / "phase.toml"
                tool.write_config(cfg_path, clip, flicker=True)
                (scene,) = cfgmod.load(str(cfg_path)).scenes
                self.assertEqual(scene.file, clip)


class AudioTest(unittest.TestCase):
    """Audio plays by default, as it does for a user, and `--no-audio` mutes."""

    def setUp(self) -> None:
        self.tool = _load("flicker_tear_ab")
        self.out = Path(tempfile.mkdtemp())

    def _audio_enabled(self, **kwargs: bool) -> bool:
        cfg_path = self.out / "phase.toml"
        self.tool.write_config(cfg_path, self.out / "ab.mkv", flicker=True, **kwargs)
        return cfgmod.load(str(cfg_path)).audio.enabled

    def test_a_phase_plays_audio_by_default(self) -> None:
        self.assertTrue(self._audio_enabled())

    def test_a_muted_phase_does_not(self) -> None:
        self.assertFalse(self._audio_enabled(audio=False))

    def _ffmpeg_inputs(self, *, audio: bool) -> list[str]:
        with mock.patch.object(self.tool.subprocess, "run") as run:
            self.tool.build_test_video(self.out / "ab.mkv", audio=audio)
        argvs = [call.args[0] for call in run.call_args_list]
        self.assertEqual(argvs[-1][-1], str(self.out / "ab.mkv"))
        return [arg for argv in argvs for arg in argv]

    def test_the_video_carries_a_tone_when_audio_plays(self) -> None:
        self.assertIn(
            f"sine=frequency={self.tool.TONE_HZ}:duration=25", self._ffmpeg_inputs(audio=True)
        )

    def test_the_muted_video_carries_none(self) -> None:
        self.assertNotIn("lavfi", self._ffmpeg_inputs(audio=False))


def _captured(picture: np.ndarray) -> np.ndarray:
    """A C64 picture as the capture device returns it: scaled up inside a
    black border."""
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[60:660, 160:1120] = cv2.resize(picture, (960, 600), interpolation=cv2.INTER_NEAREST)
    return frame


class PositionContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load("flicker_tear_ab")

    def _states(self, picture: np.ndarray) -> list[str]:
        return self.tool._position_row_states(_captured(picture))

    def test_frames_two_apart_differ(self) -> None:
        a, b = self.tool.position_state(1), self.tool.position_state(3)
        self.assertFalse(np.array_equal(a, b))

    def test_a_whole_frame_is_not_torn(self) -> None:
        frames = [_captured(self.tool.position_state(i)) for i in range(4)]
        pct, torn, blank, _ = self.tool.score(frames, self.tool._position_row_states)
        self.assertEqual((pct, torn, blank), (0.0, [], 0))
        self.assertEqual(
            {s for s in self._states(self.tool.position_state(2)) if s != "blank"}, {"2"}
        )

    def test_frames_two_apart_in_one_picture_read_as_two_apart(self) -> None:
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(3)[:96]
        frames = [_captured(picture)]
        _, torn, _, _ = self.tool.score(frames, self.tool._position_row_states)
        self.assertEqual(torn, [0])
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "two-apart")

    def test_a_ragged_seam_between_frames_two_apart_reads_as_two_apart(self) -> None:
        # What a write caught by the beam leaves: rows lighting both bands.
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(3)[:96]
        picture[96:120] |= self.tool.position_state(3)[96:120]
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "two-apart")

    def test_consecutive_frames_in_one_picture_read_as_adjacent(self) -> None:
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(2)[:96]
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "adjacent")

    def test_a_cleared_band_reads_as_partial(self) -> None:
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(1)[:96] * 0
        picture[:96, :8] = self.tool.position_state(1)[:96, :8]
        picture[:96, -8:] = self.tool.position_state(1)[:96, -8:]
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "partial")


def _mhires_picture(
    planes: dict, rows: slice = slice(None), over: dict | None = None
) -> np.ndarray:
    """What the VIC draws from `planes`, with `over`'s values in `rows` of
    character rows for each of its keys, and `over`'s bg0 if it has one."""
    from c64cast.video.palette import HOST_PALETTES

    table = np.array(HOST_PALETTES["u64"], np.uint8)
    mixed = {
        "bitmap": planes["bitmap"].reshape(25, 40, 8).copy(),
        "screen": planes["screen"].reshape(25, 40).copy(),
        "color": planes["color"].reshape(25, 40).copy(),
    }
    over = over or {}
    for key, value in over.items():
        if key != "bg":
            mixed[key][rows] = value.reshape(mixed[key].shape)[rows]
    bitmap, screen, color = mixed["bitmap"], mixed["screen"], mixed["color"]
    bg = over.get("bg", planes["bg"])
    codes = (bitmap[..., None] >> np.array([6, 4, 2, 0], np.uint8)) & 3  # (25, 40, 8, 4)
    slots = np.stack([np.full_like(screen, bg), screen >> 4, screen & 15, color & 15], axis=-1)
    index = np.take_along_axis(slots[:, :, None, :], codes.astype(np.intp), axis=3)
    picture = table[index.transpose(0, 2, 1, 3).reshape(200, 160)]
    return np.repeat(picture, 2, axis=1)


class PlaneContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load("flicker_tear_ab")

    def _report(self, *pictures: np.ndarray) -> dict:
        return self.tool.score_planes([_captured(p) for p in pictures])

    def _torn(self, report: dict) -> dict:
        return {zone: kinds for zone, kinds in report["kinds"].items() if kinds}

    def test_whole_frames_tear_nowhere_and_every_plane_shows_the_same_slot(self) -> None:
        report = self._report(*(_mhires_picture(self.tool.plane_frame(n)) for n in range(4)))
        self.assertEqual(self._torn(report), {})
        self.assertEqual(report["blank"], 0)
        for zone, offsets in report["offsets"].items():
            self.assertEqual(offsets, [0, 0, 0, 0], zone)
        self.assertEqual(report["flips"], 3)

    def test_a_frame_torn_near_the_top_is_placed_by_the_slot_most_rows_show(self) -> None:
        shown, late = self.tool.plane_frame(2), self.tool.plane_frame(1)
        report = self._report(_mhires_picture(shown, slice(0, 4), {"bitmap": late["bitmap"]}))
        self.assertEqual(self._torn(report), {"bitmap": ["adjacent"]})
        self.assertEqual(report["offsets"]["screen"], [0])

    def test_a_push_into_the_bank_on_screen_tears_bitmap_and_screen_two_apart(self) -> None:
        shown, pushed = self.tool.plane_frame(1), self.tool.plane_frame(3)
        bank = {"bitmap": pushed["bitmap"], "screen": pushed["screen"]}
        report = self._report(_mhires_picture(shown, slice(0, 12), bank))
        self.assertEqual(self._torn(report), {"bitmap": ["two-apart"], "screen": ["two-apart"]})

    def test_a_color_ram_write_caught_by_the_beam_tears_only_color(self) -> None:
        shown, pushed = self.tool.plane_frame(1), self.tool.plane_frame(2)
        report = self._report(_mhires_picture(shown, slice(0, 12), {"color": pushed["color"]}))
        self.assertEqual(self._torn(report), {"color": ["adjacent"]})

    def test_a_bg0_ahead_of_the_bank_is_an_offset_not_a_tear(self) -> None:
        shown = self.tool.plane_frame(1)
        report = self._report(_mhires_picture(shown, over={"bg": self.tool.plane_frame(2)["bg"]}))
        self.assertEqual(self._torn(report), {})
        self.assertEqual(report["offsets"]["bg0"], [1])
        self.assertEqual(report["offsets"]["screen"], [0])


class PlanesPushTest(unittest.TestCase):
    """The launched c64cast's push, with `cli.run` standing in for playback."""

    def setUp(self) -> None:
        self.tool = _load("flicker_tear_ab")

    def _run(self, buffers: dict, pushes: int) -> tuple[list[dict], str]:
        from c64cast.video.modes.mhires import MultiHiresDisplayMode

        def playback(_args: list[str]) -> int:
            for _ in range(pushes):
                MultiHiresDisplayMode.push(None, "api", buffers)  # type: ignore[arg-type]
            return 0

        composed = mock.Mock()
        clock = mock.Mock(monotonic=mock.Mock(side_effect=[0.0, 5.0, 10.0]))
        out = io.StringIO()
        with (
            mock.patch.object(MultiHiresDisplayMode, "push", composed),
            mock.patch("c64cast.app.cli.run", playback),
            mock.patch.object(self.tool, "time", clock),
            redirect_stdout(out),
        ):
            self.tool.run_app_with_planes([])
        return [c.args[2] for c in composed.call_args_list], out.getvalue()

    def _composed(self) -> dict:
        frame = self.tool.plane_frame(7)
        return {**{k: np.ones_like(v) for k, v in frame.items() if k != "bg"}, "bg": 9}

    def test_every_page_the_flicker_push_writes_carries_the_planes_frame(self) -> None:
        composed = {**self._composed(), "screen_b": np.full(1000, 0xEE, np.uint8), "text": "t"}
        (sent,) = self._run(composed, 1)[0]
        frame = self.tool.plane_frame(0)
        for key in ("bitmap", "screen", "color"):
            np.testing.assert_array_equal(sent[key], frame[key], key)
        np.testing.assert_array_equal(sent["screen_b"], frame["screen"])
        self.assertEqual((sent["bg"], sent["text"]), (frame["bg"], "t"))

    def test_the_plain_push_gets_no_second_page(self) -> None:
        (sent,) = self._run(self._composed(), 1)[0]
        self.assertNotIn("screen_b", sent)

    def test_the_report_reads_the_push_rate_the_app_prints(self) -> None:
        sent, log = self._run(self._composed(), 2 * self.tool._RATE_EVERY + 1)
        self.assertEqual(len(sent), 2 * self.tool._RATE_EVERY + 1)
        frames = [_captured(_mhires_picture(self.tool.plane_frame(n))) for n in range(2)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.tool.print_planes_report("plain", frames, 1.0, log, Path(tempfile.mkdtemp()))
        self.assertIn("1.0 page flips/s seen, 30.0 pushes/s sent", out.getvalue())


if __name__ == "__main__":
    unittest.main()
