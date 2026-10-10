"""Tests for the InterstitialScene "UP NEXT" splash + its color resolver.

Drives setup()/process_frame()/teardown() against the shared FakeAPI (no U64),
asserting the VIC mode-switch writes, centered text placement into screen +
color RAM, the duration cutoff, and the _resolve_line_colors policy
(rainbow / random / named / unknown-fallback). The parallax background itself
is covered by test_backgrounds.py; here we only check the text overlay lands
on top of whatever the background produced.
"""

# pyright: reportArgumentType=false
from __future__ import annotations

import unittest
from typing import cast

import numpy as np
from _fakes import FakeAPI, lose_writes_to

from c64cast.app.config import InterstitialCfg
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA1, VECTORS
from c64cast.hw.delivery import CONFIRM_TRIES
from c64cast.scenes.interstitial import (
    LABEL,
    LEGIBLE_COLORS,
    RAINBOW_COLORS,
    InterstitialScene,
    _resolve_line_colors,
    default_factory,
)
from c64cast.scenes.overlays import ascii_to_screen
from c64cast.video.palette import C64_COLORS


def _api() -> C64Backend:
    return cast(C64Backend, FakeAPI())


class ResolveLineColorsTest(unittest.TestCase):
    def test_rainbow_cycles_per_line(self):
        colors = _resolve_line_colors("rainbow", 3)
        self.assertEqual(colors, [RAINBOW_COLORS[i] for i in range(3)])

    def test_rainbow_wraps_past_palette_length(self):
        n = len(RAINBOW_COLORS) + 2
        colors = _resolve_line_colors("rainbow", n)
        self.assertEqual(colors[-1], RAINBOW_COLORS[(n - 1) % len(RAINBOW_COLORS)])

    def test_random_is_one_legible_color_for_all_lines(self):
        colors = _resolve_line_colors("random", 4)
        self.assertEqual(len(set(colors)), 1)
        self.assertIn(colors[0], LEGIBLE_COLORS)

    def test_named_color(self):
        self.assertEqual(_resolve_line_colors("cyan", 2), [C64_COLORS["cyan"]] * 2)

    def test_unknown_color_warns_and_uses_white(self):
        with self.assertLogs("c64cast.scenes.interstitial", level="WARNING"):
            colors = _resolve_line_colors("chartreuse", 2)
        self.assertEqual(colors, [C64_COLORS["white"]] * 2)


class InterstitialSceneTest(unittest.TestCase):
    def _scene(self, **cfg_kw):
        cfg = InterstitialCfg(**cfg_kw)
        api = _api()
        scene = InterstitialScene(api, "Webcam Show", cfg)
        return scene, api

    def test_setup_writes_vic_mode_and_centers_text(self):
        scene, api = self._scene(text_color="white", background="none")
        scene.setup()
        fake = cast(FakeAPI, api)
        # Standard PETSCII char mode + black border/bg.
        self.assertEqual(fake.memories["D018"], "14")
        self.assertEqual(fake.memories["D016"], "08")
        self.assertEqual(fake.memories["D011"], "1b")
        self.assertEqual(fake.regs["D020"], (0x00, 0x00))
        # Defeat a preceding bitmap scene's lingering bank-swap raster IRQ:
        # $0314/$0315 → kernal $EA31, IRQ source off, latched flag acked, VIC
        # bank pinned to 0. Otherwise a CTRL-skip racing the prior teardown
        # leaves the handler re-flipping $DD00 to bank 2: wrong glyphs.
        self.assertEqual(fake.regs["0314"], (0x31, 0xEA))
        self.assertEqual(fake.memories["D01A"], "00")
        self.assertEqual(fake.memories["D019"], "01")
        self.assertEqual(fake.memories["DC0D"], "81")
        self.assertEqual(fake.memories["DD00"], "97")
        self.assertEqual(fake.cache_invalidations, 1)
        self.assertEqual(scene.lines, [LABEL, "WEBCAM SHOW"])
        # 3-row block (label, blank, name) vertically centered → top at row 11.
        self.assertEqual(scene.line_rows, [11, 13])
        self.assertEqual(scene.line_cols[0], (40 - len(LABEL)) // 2)
        self.assertEqual(scene.line_cols[1], (40 - len("WEBCAM SHOW")) // 2)

    def test_process_frame_writes_text_into_screen_and_color(self):
        scene, api = self._scene(text_color="cyan", background="none")
        scene.setup()
        scene.start_time = 0.0
        still_running = scene.process_frame(0.1)
        self.assertTrue(still_running)
        fake = cast(FakeAPI, api)
        screen = np.frombuffer(fake.regions[0x0400], dtype=np.uint8)
        color = np.frombuffer(fake.regions[0xD800], dtype=np.uint8)
        base = scene.line_rows[0] * 40 + scene.line_cols[0]
        encoded = ascii_to_screen(LABEL)
        self.assertEqual(bytes(screen[base : base + len(encoded)]), encoded)
        self.assertTrue(np.all(color[base : base + len(encoded)] == C64_COLORS["cyan"]))

    def test_process_frame_returns_false_after_duration(self):
        scene, api = self._scene(duration_s=2.0, background="none")
        scene.setup()
        scene.start_time = 100.0
        self.assertTrue(scene.process_frame(101.5))
        self.assertFalse(scene.process_frame(102.0))  # elapsed >= duration

    def test_long_scene_name_is_truncated_to_width(self):
        cfg = InterstitialCfg(background="none")
        scene = InterstitialScene(_api(), "X" * 80, cfg)
        scene.setup()
        self.assertEqual(len(scene.lines[1]), 40)

    def test_teardown_is_inert(self):
        scene, api = self._scene(background="none")
        fake = cast(FakeAPI, api)
        scene.setup()
        ops_before = len(fake.ops)
        scene.teardown()  # no audio/source — must not raise
        self.assertEqual(len(fake.ops), ops_before, "an inert teardown issues no C64 writes")


class InterstitialCia1RearmTest(unittest.TestCase):
    """The card unhooks through the shared `unhook_raster_irq` sequence, its
    bank pin between the ack and the CIA #1 unmask. The unmask waits for a
    confirmed $0314 restore: unmasked with the vector still on a leaked
    handler, every jiffy IRQ would run it."""

    def _scene(self, lose: int | None = None, times: int | None = None):
        fake = FakeAPI()
        if lose is not None:
            lose_writes_to(fake, lose, times)
        scene = InterstitialScene(
            cast(C64Backend, fake), "Next", InterstitialCfg(background="none")
        )
        return scene, fake

    def test_the_card_runs_the_shared_unhook_with_its_bank_pin(self):
        scene, fake = self._scene()
        scene.setup()
        ops = [op[1:3] for op in fake.ops if op[0] in ("write_memory", "write_regs")]
        self.assertEqual(
            ops[:6],
            [
                ("DC0D", f"{CIA1.ICR_DISABLE_ALL:02X}"),
                ("D01A", "00"),
                ("0314", (0x31, 0xEA)),
                ("D019", "01"),
                ("DD00", "97"),
                ("DC0D", f"{CIA1.ICR_ENABLE_TIMER_A:02X}"),
            ],
        )

    def test_unconfirmed_restore_leaves_cia1_masked(self):
        scene, fake = self._scene(VECTORS.IRQ)
        with self.assertLogs("c64cast.scenes.interstitial", level="ERROR") as cm:
            scene.setup()
        self.assertEqual(fake.memories["DC0D"], f"{CIA1.ICR_DISABLE_ALL:02X}")
        self.assertTrue(any("leaving CIA #1 Timer A masked" in m for m in cm.output))
        # The rest of the card's setup still runs.
        self.assertEqual(fake.memories["DD00"], "97")

    def test_unconfirmed_unmask_is_logged(self):
        scene, fake = self._scene()
        write_memory = fake.write_memory

        def lossy_write_memory(addr: str, data_hex: str) -> None:
            write_memory(addr, data_hex)
            if str(addr).upper() == "DC0D" and data_hex == "81":
                fake.delivery_epoch += 1

        fake.write_memory = lossy_write_memory  # type: ignore[method-assign]
        with self.assertLogs("c64cast.scenes.interstitial", level="ERROR") as cm:
            scene.setup()
        self.assertTrue(any("'CIA1 unmask'" in m for m in cm.output))

    def test_unconfirmed_raster_disable_is_written_again_behind_the_restore(self):
        scene, fake = self._scene(0xD01A, CONFIRM_TRIES)
        with self.assertLogs("c64cast.scenes.interstitial", level="ERROR"):
            scene.setup()
        order = [op[1] for op in fake.ops if op[0] in ("write_memory", "write_regs", "lost")]
        restore = order.index("0314")
        self.assertEqual(order[:restore].count("D01A"), CONFIRM_TRIES)
        self.assertEqual(order[restore:].count("D01A"), 1, "a source live behind $EA31 storms")
        self.assertEqual(fake.memories["D01A"], "00")
        self.assertEqual(fake.memories["DC0D"], "81")


class DefaultFactoryTest(unittest.TestCase):
    def test_factory_mints_named_scenes(self):
        api = _api()
        make = default_factory(api, InterstitialCfg(background="none"))
        scene = make("Slideshow")
        self.assertIsInstance(scene, InterstitialScene)
        self.assertEqual(scene.next_scene_name, "Slideshow")

    def test_factory_defaults_cfg_when_none(self):
        make = default_factory(_api(), None)
        scene = make("Blank")
        self.assertIsInstance(scene.cfg, InterstitialCfg)


if __name__ == "__main__":
    unittest.main()
