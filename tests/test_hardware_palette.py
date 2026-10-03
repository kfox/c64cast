"""`[color].hardware_palette`: the palette derivation in c64cast.video.palette,
the run's pusher in c64cast.hw.hardware_palette, the reset listeners it hangs
off Ultimate64API, and the config refusals around it."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import cast
from unittest import mock

import cv2
import numpy as np
import requests

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.app.config import Config, SceneCfg
from c64cast.hw import hardware_palette as hp
from c64cast.hw.api import Ultimate64API
from c64cast.video import palette

# What the machine shows before the run: a custom .vpl, so a restore that
# fell back to the firmware's built-in table would not match it.
MACHINE = np.clip(np.asarray(palette.U64_PALETTE_BGR, dtype=np.int16) + 9, 0, 255).astype(np.uint8)
PINNED = list(palette.HARDWARE_PALETTE_PINNED)


class PaletteSwapTestCase(unittest.TestCase):
    """Puts the process-wide palette back after each test."""

    def setUp(self):
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))


def _lab(bgr: np.ndarray) -> np.ndarray:
    u8 = np.clip(bgr, 0, 255).astype(np.uint8).reshape(-1, 1, 3)
    return cv2.cvtColor(u8, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)


def _warm_image() -> np.ndarray:
    """A sunset-ish gradient: the gamut the machine's own table covers worst."""
    h, w = 100, 160
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    b = 40 + 60 * x * (1 - y)
    g = 60 + 120 * y * (1 - 0.5 * x)
    r = 160 + 90 * y
    return np.clip(np.stack(np.broadcast_arrays(b, g, r), axis=-1), 0, 255).astype(np.uint8)


class DeriveHardwarePaletteTest(PaletteSwapTestCase):
    def test_the_gray_axis_stays_the_machines_own(self):
        table = palette.derive_hardware_palette([_warm_image()], MACHINE)
        assert table is not None
        np.testing.assert_array_equal(table[PINNED], MACHINE[PINNED])

    def test_none_when_there_are_no_pixels(self):
        self.assertIsNone(palette.derive_hardware_palette([], MACHINE))

    def test_the_same_source_always_gets_the_same_palette(self):
        a = palette.derive_hardware_palette([_warm_image()], MACHINE)
        b = palette.derive_hardware_palette([_warm_image()], MACHINE)
        np.testing.assert_array_equal(a, b)

    def test_fits_the_source_better_than_the_machines_table(self):
        img = _warm_image()
        flat = img.reshape(-1, 3).astype(np.float32)
        table = palette.derive_hardware_palette([img], MACHINE)
        assert table is not None

        def error(t: np.ndarray) -> float:
            palette.set_host_palette(t, name="t")
            idx = palette.quantize_flat_for(flat, perceptual=True)
            shown = np.asarray(t, dtype=np.float32)[idx]
            return float(np.linalg.norm(_lab(shown) - _lab(flat), axis=1).mean())

        self.assertLess(error(table), 0.6 * error(MACHINE))

    def test_a_color_lands_on_the_index_named_for_it(self):
        """One distinct color yields one cluster, which takes the free index
        nearest it; every other free index keeps the machine's color."""
        orange = np.zeros((20, 20, 3), dtype=np.uint8)
        orange[:] = (30, 95, 175)  # a brighter orange than the machine's own
        table = palette.derive_hardware_palette([orange], MACHINE)
        assert table is not None
        self.assertLessEqual(int(np.abs(table[8].astype(int) - (30, 95, 175)).max()), 3)
        others = [i for i in range(16) if i != 8]
        np.testing.assert_array_equal(table[others], MACHINE[others])

    def test_gray_pixels_do_not_spend_a_free_index(self):
        img = np.zeros((20, 40, 3), dtype=np.uint8)
        img[:, :20] = (30, 95, 175)
        img[:, 20:] = MACHINE[12]
        table = palette.derive_hardware_palette([img], MACHINE)
        assert table is not None
        self.assertLessEqual(int(np.abs(table[8].astype(int) - (30, 95, 175)).max()), 3)
        others = [i for i in range(16) if i != 8]
        np.testing.assert_array_equal(table[others], MACHINE[others])

    def test_each_of_eleven_colors_takes_the_index_nearest_it(self):
        free = [i for i in range(16) if i not in PINNED]
        nudged = np.clip(MACHINE[free].astype(np.int16) + (4, -4, 4), 0, 255).astype(np.uint8)
        order = np.random.default_rng(7).permutation(len(free))
        img = np.repeat(nudged[order][None, :, :], 10, axis=0).repeat(10, axis=1)
        table = palette.derive_hardware_palette([img], MACHINE)
        assert table is not None
        self.assertLessEqual(int(np.abs(table[free].astype(int) - nudged.astype(int)).max()), 3)


class PaletteGenerationTest(PaletteSwapTestCase):
    def test_moves_when_the_colors_change_and_only_then(self):
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        before = palette.palette_generation()
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        self.assertEqual(palette.palette_generation(), before)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertEqual(palette.palette_generation(), before + 1)

    def test_mhires_rebuilds_its_pairwise_table_on_a_swap(self):
        from c64cast.video.modes import MultiHiresDisplayMode

        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        mode = MultiHiresDisplayMode("grayscale")
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        mode.compose(np.zeros((200, 160, 3), dtype=np.uint8))
        fresh = palette.quantize_distances_for(palette.C64_PALETTE_BGR, perceptual=mode._perceptual)
        np.testing.assert_array_equal(mode._pal_pairwise, fresh)

    def test_the_inverse_pop_lut_is_dropped_on_a_swap(self):
        from c64cast.video.petscii_styles import InversePopStyle

        InversePopStyle._LUT_CACHE[True] = np.zeros(16, dtype=np.uint8)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        self.assertEqual(InversePopStyle._LUT_CACHE, {})


class _FakeApi:
    """The parts of Ultimate64API the pusher touches."""

    hardware_palette: object = None

    def __init__(self) -> None:
        self.listeners: list = []

    def add_reset_listener(self, callback) -> None:
        self.listeners.append(callback)

    def remove_reset_listener(self, callback) -> None:
        if callback in self.listeners:
            self.listeners.remove(callback)


def _rgb(table_bgr: np.ndarray) -> list[tuple[int, int, int]]:
    return [(int(r), int(g), int(b)) for b, g, r in table_bgr]


class HardwarePaletteTest(PaletteSwapTestCase):
    def setUp(self):
        super().setUp()
        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        self.api = _FakeApi()
        self.pushes: list[list[tuple[int, int, int]]] = []
        self.answers: list[bool] = []
        patcher = mock.patch.object(hp.uci, "set_palette_rgb", side_effect=self._set)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.control = hp.HardwarePalette(cast(Ultimate64API, self.api), MACHINE)
        self.api.hardware_palette = self.control
        self.api.add_reset_listener(self.control.after_reset)
        self.scene_table = MACHINE.copy()
        self.scene_table[8] = (30, 95, 175)

    def _set(self, api, rgb) -> bool:
        self.pushes.append(list(rgb))
        return self.answers.pop(0) if self.answers else True

    def _show(self) -> bool:
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            return self.control.show(self.scene_table, "scene")

    def test_show_pushes_the_table_and_points_the_quantizer_at_it(self):
        self.assertTrue(self._show())
        self.assertEqual(self.pushes, [_rgb(self.scene_table)])
        np.testing.assert_array_equal(palette.C64_PALETTE_BGR, self.scene_table)

    def test_showing_the_same_table_again_does_not_push(self):
        self._show()
        self.assertTrue(self.control.show(self.scene_table, "scene"))
        self.assertEqual(len(self.pushes), 1)

    def test_show_machine_pushes_the_snapshot_and_restores_the_base(self):
        self._show()
        self.control.show_machine()
        self.assertEqual(self.pushes[-1], _rgb(MACHINE))
        np.testing.assert_array_equal(
            palette.C64_PALETTE_BGR, np.asarray(palette.PEPTO_PALETTE_BGR, dtype=np.float32)
        )
        self.assertEqual(palette.active_host_palette_name(), "pepto")

    def test_show_machine_with_nothing_shown_does_not_push(self):
        self.control.show_machine()
        self.assertEqual(self.pushes, [])

    def test_a_reset_re_pushes_what_the_scene_shows(self):
        self._show()
        self.api.listeners[0]()
        self.assertEqual(self.pushes, [_rgb(self.scene_table)] * 2)

    def test_a_reset_with_nothing_shown_does_not_push(self):
        self.api.listeners[0]()
        self.assertEqual(self.pushes, [])

    def test_a_reset_after_the_scene_ended_does_not_push(self):
        self._show()
        self.control.show_machine()
        self.api.listeners[0]()
        self.assertEqual(len(self.pushes), 2)

    def test_restore_pushes_the_snapshot_never_the_built_in_table(self):
        """RESET_PALETTE would load the firmware's default table, which on a
        machine running a .vpl is not what it was showing."""
        self._show()
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            self.control.restore()
        self.assertEqual(self.pushes[-1], _rgb(MACHINE))
        self.assertEqual(self.api.listeners, [])
        self.assertIsNone(self.api.hardware_palette)
        self.assertEqual(palette.active_host_palette_name(), "pepto")

    def test_restore_after_a_reset_still_pushes_when_a_scene_was_showing(self):
        self._show()
        self.api.listeners[0]()
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            self.control.restore()
        self.assertEqual(self.pushes[-1], _rgb(MACHINE))

    def test_restore_with_nothing_pushed_does_nothing(self):
        self.control.restore()
        self.assertEqual(self.pushes, [])

    def test_a_push_that_fails_once_is_retried(self):
        self.answers = [False, True]
        self.assertTrue(self._show())
        self.assertEqual(len(self.pushes), 2)

    def test_a_push_that_fails_twice_turns_pushing_off(self):
        self.answers = [False, False]
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING") as logs:
            self.assertFalse(self.control.show(self.scene_table, "scene"))
        self.assertIn("rest of the run", logs.output[0])
        self.assertEqual(palette.active_host_palette_name(), "pepto")
        self.assertFalse(self.control.show(self.scene_table, "scene"))
        self.api.listeners[0]()
        self.assertEqual(len(self.pushes), 2)

    def test_restore_still_tries_after_a_failed_push(self):
        """The machine's answer to the failed push was lost, not necessarily
        the push itself."""
        self.answers = [False, False]
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING"):
            self.control.show(self.scene_table, "scene")
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            self.control.restore()
        self.assertEqual(self.pushes[-1], _rgb(MACHINE))

    def test_a_restore_that_fails_says_so(self):
        self._show()
        self.answers = [False, False]
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING") as logs:
            self.control.restore()
        self.assertIn("could not put", logs.output[0])


def _cfg(*scenes: SceneCfg, source: bool = True) -> Config:
    cfg = Config()
    cfg.color.hardware_palette = "source" if source else "off"
    cfg.scenes = list(scenes)
    return cfg


def _u64() -> mock.MagicMock:
    api = mock.MagicMock(spec=Ultimate64API)
    api.profile = mock.Mock(supports_system_mode=True)
    api.hardware_palette = None
    return api


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("c64cast.hw.hw_provision.read_active_palette")
        self.read = patcher.start()
        self.addCleanup(patcher.stop)
        self.read.return_value = tuple(map(tuple, MACHINE))

    def test_nothing_happens_when_no_scene_asks(self):
        cfg = _cfg(SceneCfg(type="video"), source=False)
        self.assertIsNone(hp.provision_hardware_palette(_u64(), cfg, is_ensemble=False))
        self.read.assert_not_called()

    def test_a_scene_type_that_cannot_push_does_not_count(self):
        cfg = _cfg(SceneCfg(type="webcam"))
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO") as logs:
            self.assertIsNone(hp.provision_hardware_palette(_u64(), cfg, is_ensemble=False))
        self.assertIn("webcam", logs.output[0])
        self.read.assert_not_called()

    def test_a_scene_override_asks_on_its_own(self):
        cfg = _cfg(SceneCfg(type="slideshow", color={"hardware_palette": "source"}), source=False)
        api = _u64()
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            control = hp.provision_hardware_palette(api, cfg, is_ensemble=False)
        self.assertIsNotNone(control)

    def test_installs_the_pusher_and_its_reset_listener(self):
        api = _u64()
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            control = hp.provision_hardware_palette(
                api, _cfg(SceneCfg(type="video")), is_ensemble=False
            )
        assert control is not None
        self.assertIs(api.hardware_palette, control)
        api.add_reset_listener.assert_called_once_with(control.after_reset)
        np.testing.assert_array_equal(control.machine_palette, MACHINE)

    def test_firmware_without_the_command_falls_back_with_a_warning(self):
        """C64U 1.1.0 and pre-3.15 firmware answer 21,UNKNOWN COMMAND, which
        the read reports as None."""
        self.read.return_value = None
        api = _u64()
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING") as logs:
            control = hp.provision_hardware_palette(
                api, _cfg(SceneCfg(type="video")), is_ensemble=False
            )
        self.assertIsNone(control)
        self.assertIn("did not answer", logs.output[0])
        self.assertEqual(self.read.call_count, 2)
        api.add_reset_listener.assert_not_called()

    def test_a_read_that_fails_once_is_retried(self):
        self.read.side_effect = [None, tuple(map(tuple, MACHINE))]
        with self.assertLogs("c64cast.hw.hardware_palette", level="INFO"):
            control = hp.provision_hardware_palette(
                _u64(), _cfg(SceneCfg(type="video")), is_ensemble=False
            )
        self.assertIsNotNone(control)

    def _assert_skipped(self, api, cfg, *, is_ensemble=False, reason: str) -> None:
        with self.assertLogs("c64cast.hw.hardware_palette", level="WARNING") as logs:
            self.assertIsNone(hp.provision_hardware_palette(api, cfg, is_ensemble=is_ensemble))
        self.assertIn(reason, logs.output[0])
        self.read.assert_not_called()

    def test_skipped_on_a_machine_that_is_not_an_ultimate_64(self):
        self._assert_skipped(object(), _cfg(SceneCfg(type="video")), reason="Ultimate 64")
        api = _u64()
        api.profile.supports_system_mode = False
        self._assert_skipped(api, _cfg(SceneCfg(type="video")), reason="Ultimate 64")

    def test_skipped_under_skip_probe(self):
        cfg = _cfg(SceneCfg(type="video"))
        cfg.debug.skip_probe = True
        self._assert_skipped(_u64(), cfg, reason="--skip-probe")

    def test_skipped_in_an_ensemble(self):
        self._assert_skipped(
            _u64(), _cfg(SceneCfg(type="video")), is_ensemble=True, reason="ensemble"
        )


class ConfigRefusalTest(unittest.TestCase):
    def test_a_bad_value_is_refused(self):
        cfg = Config()
        cfg.color.hardware_palette = "sauce"
        with self.assertRaises(cfgmod.ConfigError):
            scene_factory.validate_hardware_palette_cfg(cfg)

    def test_refused_alongside_force_palette(self):
        cfg = _cfg()
        cfg.color.force_palette = True
        with self.assertRaisesRegex(cfgmod.ConfigError, "force_palette"):
            scene_factory.validate_hardware_palette_cfg(cfg)

    def test_refused_alongside_flicker_blending(self):
        cfg = Config()
        cfg.scenes = [
            SceneCfg(
                type="video", color={"hardware_palette": "source", "flicker_tolerance": "clean"}
            )
        ]
        with self.assertRaisesRegex(cfgmod.ConfigError, r"\[\[scenes\]\]\[0\].*flicker"):
            scene_factory.validate_hardware_palette_cfg(cfg)

    def test_source_on_its_own_passes_and_is_run_by_the_session(self):
        scene_factory.validate_hardware_palette_cfg(_cfg(SceneCfg(type="video")))
        self.assertIn(
            scene_factory.validate_hardware_palette_cfg, scene_factory.PER_SYSTEM_VALIDATORS
        )


class SlideshowPushTest(unittest.TestCase):
    """The slideshow pushes per image, while it is the scene on screen."""

    def setUp(self):
        from c64cast.app.config import ColorCfg
        from c64cast.scenes.scenes import SlideshowScene

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, color in (("a.png", (30, 95, 175)), ("b.png", (200, 60, 40))):
            img = np.zeros((40, 64, 3), dtype=np.uint8)
            img[:] = color
            cv2.imwrite(os.path.join(tmp.name, name), img)
        self.control = mock.Mock(spec=hp.HardwarePalette)
        self.control.machine_palette = MACHINE.copy()
        api = mock.MagicMock()
        api.hardware_palette = self.control
        mode = mock.MagicMock()
        mode.quantizer_input.side_effect = lambda img: img
        self.scene = SlideshowScene(
            api, mode, tmp.name, color=ColorCfg(hardware_palette="source", auto_fit=False)
        )

    def test_the_up_next_pick_does_not_push(self):
        self.scene.prepare_next()
        self.control.show.assert_not_called()

    def test_setup_pushes_and_each_new_image_pushes_again(self):
        self.scene.prepare_next()
        self.scene.setup()
        self.assertEqual(self.control.show.call_count, 1)
        self.scene._image_start -= self.scene.image_duration_s
        with mock.patch("c64cast.scenes.scenes._render_with_overlays"):
            self.scene.process_frame(self.scene._image_start + self.scene.image_duration_s)
        self.assertEqual(self.control.show.call_count, 2)
        first, second = (c.args[0] for c in self.control.show.call_args_list)
        self.assertFalse(np.array_equal(first, second))

    def test_teardown_puts_the_machines_palette_back(self):
        self.scene.setup()
        self.scene.teardown()
        self.control.show_machine.assert_called_once()

    def test_off_never_touches_the_pusher(self):
        self.scene._color.hardware_palette = "off"
        self.scene.setup()
        self.scene.teardown()
        self.control.show.assert_not_called()
        self.control.show_machine.assert_not_called()


class VideoPushTest(unittest.TestCase):
    """A video fits its palette from the pre-scan, after the fit is installed."""

    def setUp(self):
        from c64cast.app.config import ColorCfg
        from c64cast.scenes.scenes import VideoScene

        self.control = mock.Mock(spec=hp.HardwarePalette)
        self.control.machine_palette = MACHINE.copy()
        api = mock.MagicMock()
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
        self.fit = object()
        patches = {
            "ensure_pyav": mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            "source": mock.patch("c64cast.scenes.scenes.AVFileSource"),
            "prescan": mock.patch(
                "c64cast.scenes.scenes.prescan_source_color", side_effect=self._prescan
            ),
        }
        started = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        self.prescan = started["prescan"]

    def _prescan(self, path, **kw):
        kw["frames"].add(_warm_image())
        # The palette is fitted to frames shaped with this fit, so it has to
        # be installed before the push.
        self.mode.quantizer_input.side_effect = self._shaped
        return self.fit, None

    def _shaped(self, img):
        self.assertIs(self.mode.set_color_fit.call_args.args[0], self.fit)
        return img

    def test_setup_pre_scans_and_pushes_the_fitted_palette(self):
        self.scene.setup()
        self.assertIsNotNone(self.prescan.call_args.kwargs["frames"])
        self.control.show.assert_called_once()
        table = self.control.show.call_args.args[0]
        np.testing.assert_array_equal(table[PINNED], MACHINE[PINNED])

    def test_teardown_puts_the_machines_palette_back(self):
        self.scene.setup()
        self.scene.teardown()
        self.control.show_machine.assert_called_once()


class ResetListenerTest(unittest.TestCase):
    """Ultimate64API runs its reset listeners after every reset it issues."""

    def setUp(self):
        patcher = mock.patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = Ultimate64API("http://example.invalid")
        self.calls = 0
        self.api.add_reset_listener(self._listener)
        for name in ("blank_display", "flush", "invalidate_cache", "_flush_or_raise"):
            p = mock.patch.object(self.api, name)
            p.start()
            self.addCleanup(p.stop)

    def _listener(self) -> None:
        self.calls += 1

    def test_reset_runs_the_listeners(self):
        with mock.patch.object(self.api.session, "put"):
            self.api.reset()
        self.assertEqual(self.calls, 1)

    def test_a_failed_reset_does_not(self):
        with (
            mock.patch.object(self.api.session, "put", side_effect=requests.ConnectionError()),
            self.assertLogs("c64cast.hw.api", level="WARNING"),
        ):
            self.api.reset()
        self.assertEqual(self.calls, 0)

    def test_the_clear_loop_run_prg_runs_them(self):
        with mock.patch.object(self.api.session, "post"):
            self.api.run_basic_clear_loop()
        self.assertEqual(self.calls, 1)

    def test_every_runner_kick_runs_them(self):
        with mock.patch.object(self.api.session, "post"):
            self.api._post_prg("/v1/runners:run_prg", "x.prg", b"\x01\x08", timeout=1, what="")
        self.assertEqual(self.calls, 1)

    def test_a_removed_listener_is_not_run(self):
        self.api.remove_reset_listener(self._listener)
        with mock.patch.object(self.api.session, "put"):
            self.api.reset()
        self.assertEqual(self.calls, 0)

    def test_a_listener_that_raises_is_logged_and_the_rest_still_run(self):
        self.api.remove_reset_listener(self._listener)
        self.api.add_reset_listener(mock.Mock(side_effect=RuntimeError("boom")))
        self.api.add_reset_listener(self._listener)
        with (
            mock.patch.object(self.api.session, "put"),
            self.assertLogs("c64cast.hw.api", level="ERROR"),
        ):
            self.api.reset()
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
