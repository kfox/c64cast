"""Tests for the BigText 1×→8× scroller overlay.

The PETSCII ROM may not be present in the test env, so every test
constructs the overlay with charset_path="" — the loader falls back to
the framebuffer's builtin glyph generator, which is sufficient for
exercising every code path we care about.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import numpy as np

from c64cast.modes import BlankDisplayMode, MCMDisplayMode, PETSCIIDisplayMode
from c64cast.overlays import validate_for_scene
from c64cast.overlays.big_text import BigTextOverlay


def _make_buffers():
    """Fresh empty buffers shaped like CharDisplayMode.compose() returns."""
    return {
        "screen": np.full(40 * 25, 0x20, dtype=np.uint8),
        "color": np.zeros(40 * 25, dtype=np.uint8),
    }


def _make_overlay(**overrides):
    kwargs = {
        "messages": [{"text": "HI", "color": "yellow"}],
        "charset_path": "",   # force builtin-charset fallback
        "speed_cells_per_s": 20.0,
        "inter_message_pause_s": 0.1,
    }
    kwargs.update(overrides)
    return BigTextOverlay(**kwargs)


class ConstructionTest(unittest.TestCase):

    def test_empty_messages_raises(self):
        with self.assertRaises(ValueError):
            BigTextOverlay(messages=[], charset_path="")

    def test_unknown_color_raises(self):
        with self.assertRaises(ValueError):
            _make_overlay(messages=[{"text": "X", "color": "magenta"}])

    def test_unknown_message_keys_raises(self):
        # The simplified overlay only accepts {text, color} — extras reject.
        with self.assertRaises(ValueError):
            _make_overlay(messages=[{"text": "X", "size": 3}])

    def test_message_missing_text_raises(self):
        with self.assertRaises(ValueError):
            _make_overlay(messages=[{"color": "white"}])

    def test_bad_row_raises(self):
        with self.assertRaises(ValueError):
            _make_overlay(row="diagonal")

    def test_paints_into_buffers(self):
        # Compose-time overlay; the Playlist must skip per-frame process_frame.
        ov = _make_overlay()
        self.assertTrue(ov.PAINTS_INTO_BUFFERS)

    def test_compatible_modes(self):
        # blank + mcm only — bitmap modes and petscii are out.
        self.assertEqual(BigTextOverlay.COMPATIBLE_MODES, ("blank", "mcm"))


class ValidationTest(unittest.TestCase):

    def test_blank_mode_accepted(self):
        ov = _make_overlay()
        validate_for_scene(ov, BlankDisplayMode())

    def test_mcm_mode_accepted(self):
        ov = _make_overlay()
        validate_for_scene(ov, MCMDisplayMode())

    def test_petscii_mode_rejected(self):
        # petscii would let big-text-painted cells fight with the scene's
        # own PETSCII glyph rendering. Plan keeps petscii off the whitelist.
        ov = _make_overlay()
        with self.assertRaises(ValueError):
            validate_for_scene(ov, PETSCIIDisplayMode())


class GlyphBitsTest(unittest.TestCase):
    """The source-pixel table is (8, n*8) — 8 rows tall, 8 columns per
    source char. This is what gets sampled 1:1 into screen cells."""

    def test_glyph_bits_shape(self):
        ov = _make_overlay(messages=[{"text": "ABCDE", "color": "white"}])
        bits = ov._glyph_bits(0)
        self.assertEqual(bits.shape, (8, 5 * 8))
        self.assertEqual(bits.dtype, bool)


class RenderTest(unittest.TestCase):
    """compose() should paint big-text cells once the message has scrolled
    on-screen. With cell-snap render + D016 X-scroll, the painted cells
    are always SC=$A0 (blank mode) or SC=$FF (mcm mode)."""

    def _scene(self, mode):
        s = MagicMock()
        s.display_mode = mode
        return s

    def test_blank_compose_writes_strip_via_page_flip(self):
        # Blank mode uses page-flipping: it does NOT mutate buffers["screen"]
        # (so BlankDisplayMode.push() stays a no-op vs its diff cache) and
        # instead writes the 8-row strip's bytes directly to the offscreen
        # page ($0400 or $0C00) via write_memory_file, then flips D018.
        api = MagicMock()
        ov = _make_overlay(messages=[{"text": "HI", "color": "yellow"}],
                          row="middle", speed_cells_per_s=80.0)
        ov.setup(api=api, scene=self._scene(BlankDisplayMode()))
        ov.compose(_make_buffers(), self._scene(BlankDisplayMode()), 0.0)
        for t_step in (0.2, 0.4, 0.6, 1.0, 2.0):
            ov.compose(_make_buffers(), self._scene(BlankDisplayMode()),
                       t_step)
        # Page-flip writes target the strip area of each page. Strip starts
        # at row 8 of a 25-row screen → cell offset 320 → $page+$140.
        strip_calls = [c for c in api.write_memory_file.call_args_list
                       if c.args[0] in ("0540", "0D40")]
        self.assertTrue(strip_calls,
            "expected at least one strip page-flip write")
        # Cell-expansion only writes SC=$A0 ("on") or SC=$20 ("off") —
        # no other chars.
        for call in strip_calls:
            data = call.args[1]
            self.assertTrue(all(b in (0x20, 0xA0) for b in data),
                "page-flip strip write must contain only $20/$A0")
        # At least one frame should have lit cells (the text scrolls into
        # view by t=2.0, even at speed=80 cells/s).
        self.assertTrue(any(0xA0 in c.args[1] for c in strip_calls),
            "lit cells must use SC=$A0 once text is on-screen")

    def test_mcm_compose_paints_with_multicolor_flag(self):
        ov = _make_overlay(messages=[{"text": "HI", "color": "white"}],
                          speed_cells_per_s=80.0)
        ov.setup(api=MagicMock(), scene=self._scene(MCMDisplayMode()))
        for t_step in (0.2, 0.4, 0.6, 1.0, 2.0):
            buffers = _make_buffers()
            ov.compose(buffers, self._scene(MCMDisplayMode()), t_step)
            lit = buffers["screen"] == 0xFF
            if lit.any():
                # The whole 8-row strip's color RAM gets the multicolor flag
                # (so unlit/SC_SPACE cells in the strip stay invisible at
                # the FG color too — keeps color RAM constant across scroll
                # frames). Lit cells (SC=$FF) must have bit 3 set.
                self.assertTrue((buffers["color"][lit] & 0x08).all(),
                                "MCM lit big-text cells must have color bit 3 set")
                return
        self.fail("text never appeared on screen after several seconds")

    def test_animation_advances_over_time(self):
        # Two well-separated snapshots should produce different page-flip
        # writes (the strip's screen-RAM bytes differ as text scrolls).
        # Motion is frame-counted now (one px-per-frame chunk per
        # compose() call), so we drive enough frames to put the text
        # into different on-screen positions.
        api = MagicMock()
        ov = _make_overlay(messages=[{"text": "ABCDE", "color": "white"}],
                          speed_cells_per_s=10.0)
        ov.setup(api=api, scene=self._scene(BlankDisplayMode()))

        def drive(n_frames, t_start):
            for i in range(n_frames):
                ov.compose(_make_buffers(),
                           self._scene(BlankDisplayMode()),
                           t_start + i * 0.02)

        # Get past the off-right blank phase, then snapshot strip bytes.
        drive(80, 0.0)
        api.write_memory_file.reset_mock()
        drive(40, 1.6)
        strips_t1 = [c for c in api.write_memory_file.call_args_list
                     if c.args[0] in ("0540", "0D40")]
        api.write_memory_file.reset_mock()
        drive(40, 2.4)
        strips_t2 = [c for c in api.write_memory_file.call_args_list
                     if c.args[0] in ("0540", "0D40")]
        self.assertTrue(strips_t1, "text should be writing strip by t=1.6")
        self.assertTrue(strips_t2, "text should still be writing strip by t=2.4")
        # Bytes from the two snapshots should differ (text scrolled).
        bytes_t1 = strips_t1[-1].args[1]
        bytes_t2 = strips_t2[-1].args[1]
        self.assertNotEqual(bytes_t1, bytes_t2,
            "strip bytes at different t should differ as text scrolls")


class SmoothScrollTest(unittest.TestCase):
    """compose() updates the shadow X-scroll byte ($C100) each frame; a
    raster IRQ on the C64 commits it into $D016 during VBLANK. Tearing-
    free, so motion is pixel-smooth between cell-aligned screen updates."""

    def _scene(self, mode):
        s = MagicMock()
        s.display_mode = mode
        return s

    def test_shadow_d016_written_each_sub_cell_step(self):
        api = MagicMock()
        ov = _make_overlay(messages=[{"text": "X", "color": "white"}],
                          speed_cells_per_s=8.0)
        ov.setup(api=api, scene=self._scene(BlankDisplayMode()))
        # Drive 8 frames spaced 1 px of scroll apart. Each frame should
        # produce a different X-scroll byte, written to the shadow address
        # at $C100 (the raster IRQ handler reads from there).
        seen = set()
        for i in range(8):
            ov.compose(_make_buffers(), self._scene(BlankDisplayMode()),
                       float(i) / 64.0)
            # Look at the most recent write_memory call for $C100 (case-
            # insensitive, since the U64 accepts either).
            for call in reversed(api.write_memory.call_args_list):
                args, _ = call
                if args and args[0].lower() == "c100":
                    seen.add(args[1])
                    break
        # We expect at least 4 distinct X-scroll bytes across the 8 steps.
        # (Per-frame quantization may produce same byte for consecutive
        # frames; 4 unique is a conservative floor.)
        self.assertGreaterEqual(len(seen), 4,
            f"expected several distinct shadow-D016 writes, got {seen}")

    def test_raster_irq_installed_on_setup(self):
        # setup() must upload the 6502 raster IRQ handler to $C000 and
        # hook the IRQ vector at $0314/$0315 to it. Without this, the
        # shadow writes in compose() would have nowhere to be committed
        # from and $D016/$D018 would never actually change.
        api = MagicMock()
        ov = _make_overlay()
        ov.setup(api=api, scene=self._scene(BlankDisplayMode()))
        # Handler uploaded to $C000.
        handler_uploads = [c for c in api.write_memory_file.call_args_list
                           if c.args[0].lower() == "c000"]
        self.assertTrue(handler_uploads,
            "expected the raster IRQ handler to be uploaded to $C000")
        # IRQ vector at $0314 swung to $C000.
        vector_writes = [c for c in api.write_regs.call_args_list
                         if c.args[0] == "0314"]
        self.assertTrue(vector_writes,
            "expected the IRQ vector $0314/$0315 to be hooked")
        # Vector low byte = $00, high byte = $C0 → points at $C000.
        last_vector = vector_writes[-1].args
        self.assertEqual((last_vector[1], last_vector[2]), (0x00, 0xC0))

    def test_raster_irq_uninstalled_on_teardown(self):
        # teardown() must restore the kernal IRQ vector ($EA31) so the
        # next scene doesn't run with our handler still hooked.
        api = MagicMock()
        ov = _make_overlay()
        scene = self._scene(BlankDisplayMode())
        ov.setup(api=api, scene=scene)
        api.write_regs.reset_mock()
        ov.teardown(api=api, scene=scene)
        vector_writes = [c for c in api.write_regs.call_args_list
                         if c.args[0] == "0314"]
        self.assertTrue(vector_writes,
            "expected teardown to restore the IRQ vector $0314")
        last_vector = vector_writes[-1].args
        # Kernal default IRQ handler is at $EA31.
        self.assertEqual((last_vector[1], last_vector[2]), (0x31, 0xEA))


class IsBusyTest(unittest.TestCase):
    """is_busy() in one-shot (loop=False) mode reports True while messages
    are still in flight, False once every message has run through."""

    def test_busy_until_messages_exhausted(self):
        ov = _make_overlay(messages=[
            {"text": "A"},
            {"text": "B"},
        ], speed_cells_per_s=400.0,
            inter_message_pause_s=0.0, loop=False)
        ov.setup(api=MagicMock(), scene=MagicMock())
        self.assertTrue(ov.is_busy())
        for t in np.arange(0.0, 10.0, 0.05):
            ov.compose(_make_buffers(), MagicMock(), float(t))
        self.assertFalse(ov.is_busy(),
                         "should not be busy after all messages scrolled off")

    def test_busy_true_at_start_with_pending_message(self):
        ov = _make_overlay(messages=[{"text": "HELLO"}], loop=False)
        self.assertTrue(ov.is_busy())


class ColorCycleTest(unittest.TestCase):
    """SHIFT-driven color cycle. Starts at 'config' (per-message color),
    then advances through rainbow + each spectrum entry."""

    def test_cycle_returns_label_and_advances(self):
        from c64cast.overlays.big_text import COLOR_CYCLE, COLOR_CYCLE_LABELS
        ov = _make_overlay()
        # Initial state is index 0 = "config"; first cycle moves to "rainbow".
        seen = []
        for _ in range(len(COLOR_CYCLE)):
            label = ov.cycle_style(api=MagicMock(), scene=MagicMock())
            seen.append(label)
        # Visited every label exactly once after a full cycle, ending where
        # we started.
        self.assertEqual(set(seen), set(COLOR_CYCLE_LABELS))
        # One more advance returns to "rainbow" (index 1).
        self.assertEqual(ov.cycle_style(api=MagicMock(), scene=MagicMock()),
                         "rainbow")

    def test_default_state_uses_per_message_color(self):
        # Before any SHIFT, compose() should use msg._resolved_color as-is.
        ov = _make_overlay(messages=[{"text": "X", "color": "yellow"}])
        ov.setup(api=MagicMock(), scene=MagicMock())
        # Drive a single compose to advance the message into view.
        buffers = _make_buffers()
        scene = MagicMock()
        scene.display_mode = BlankDisplayMode()
        for t in (0.0, 0.05, 0.1, 0.2, 0.5):
            ov.compose(buffers, scene, t)
        # Without any cycle press, the strip's color RAM should contain
        # yellow (palette index 7). MCM masking doesn't apply for blank.
        from c64cast.palette import C64_COLORS
        yellow = C64_COLORS["yellow"]
        # The strip starts at the middle row and spans 8 rows × 40 cols.
        # Just check at least one cell in the strip is yellow.
        strip = buffers["color"][8 * 40:16 * 40]
        self.assertIn(yellow, strip,
                      "default state must paint the message in its config color")

    def test_cycle_overrides_per_message_color(self):
        ov = _make_overlay(messages=[{"text": "X", "color": "yellow"}])
        ov.setup(api=MagicMock(), scene=MagicMock())
        # Cycle to rainbow (1 press). Subsequent compose should paint
        # rainbow columns instead of yellow.
        label = ov.cycle_style(api=MagicMock(), scene=MagicMock())
        self.assertEqual(label, "rainbow")
        buffers = _make_buffers()
        scene = MagicMock()
        scene.display_mode = BlankDisplayMode()
        for t in (0.0, 0.05, 0.1, 0.2, 0.5):
            ov.compose(buffers, scene, t)
        # Rainbow mode fills each column with a different spectrum index,
        # so the strip's color RAM has more than one unique value (yellow
        # alone would give exactly one).
        strip = buffers["color"][8 * 40:16 * 40]
        self.assertGreater(len(set(strip.tolist())), 1,
                           "rainbow override should produce multi-color strip")

    def test_cycle_wraps_back_to_config(self):
        from c64cast.overlays.big_text import COLOR_CYCLE
        ov = _make_overlay(messages=[{"text": "X", "color": "yellow"}])
        # Advance all the way around — last label should be "config" again.
        last = None
        for _ in range(len(COLOR_CYCLE)):
            last = ov.cycle_style(api=MagicMock(), scene=MagicMock())
        self.assertEqual(last, "config",
                         "full cycle should land back at 'config'")


class FollowerComposeTest(unittest.TestCase):
    """In ensemble span-mode the follower's _compose_follower must
    paint the conductor's published color — NOT the follower's own
    local message color. Regression for the "right-most screen uses
    rainbow, every other screen uses white" bug."""

    def _scene(self, mode):
        s = MagicMock()
        s.display_mode = mode
        return s

    def _fake_orch(self, snapshot: dict):
        orch = MagicMock()
        orch.is_active.return_value = True
        orch.snapshot.return_value = snapshot
        # local_x_left_px(idx, abs_px) — return something on-screen so
        # the follower actually paints lit cells.
        orch.local_x_left_px.return_value = 0
        return orch

    def _setup_follower(self, ov: BigTextOverlay, orch):
        ov._orchestrator = orch
        ov._is_conductor = False
        ov._system_index = 0
        ov._api = MagicMock()
        ov._rainbow_spectrum = ov._rainbow_spectrum   # unchanged

    def test_follower_uses_published_color_not_local_message_color(self):
        # Conductor publishes rainbow (color = -1 sentinel). Follower's
        # own local big_text overlay was configured with color=white
        # (e.g. the placeholder follower scene in left.toml). After fix,
        # follower must paint rainbow, not white.
        from c64cast.overlays.big_text import _RAINBOW_SENTINEL
        ov = _make_overlay(messages=[{"text": "PLACEHOLDER", "color": "white"}])
        bits = np.ones((8, 16), dtype=bool)   # all-on glyph block
        orch = self._fake_orch({
            "bits": bits,
            "color": _RAINBOW_SENTINEL,
            "rainbow": True,
            "abs_scroll_px": 0,
            "px_per_frame": 1,
            "screen_w_px": 320,
        })
        self._setup_follower(ov, orch)
        buffers = _make_buffers()
        scene = self._scene(BlankDisplayMode())
        ov.compose(buffers, scene, 0.0)
        # Rainbow paints each column with a different spectrum color, so
        # the strip's color RAM should have multiple distinct values.
        # The strip starts at the middle row and spans 8 rows × 40 cols.
        strip = buffers["color"][8 * 40:16 * 40]
        self.assertGreater(len(set(strip.tolist())), 1,
                           "follower must render rainbow when conductor "
                           "published rainbow, regardless of local "
                           "message color")

    def test_follower_uses_published_solid_color(self):
        # Conductor publishes a specific color (yellow). Follower's local
        # message says white. Follower must paint yellow.
        from c64cast.palette import C64_COLORS
        yellow = C64_COLORS["yellow"]
        white = C64_COLORS["white"]
        ov = _make_overlay(messages=[{"text": "PLACEHOLDER", "color": "white"}])
        bits = np.ones((8, 16), dtype=bool)
        orch = self._fake_orch({
            "bits": bits,
            "color": yellow,
            "rainbow": False,
            "abs_scroll_px": 0,
            "px_per_frame": 1,
            "screen_w_px": 320,
        })
        self._setup_follower(ov, orch)
        buffers = _make_buffers()
        scene = self._scene(BlankDisplayMode())
        ov.compose(buffers, scene, 0.0)
        strip = buffers["color"][8 * 40:16 * 40]
        unique = set(strip.tolist())
        self.assertIn(yellow, unique,
                      "follower must paint the conductor's published color")
        self.assertNotIn(white, unique,
                         "follower must NOT use its local message color")


class LoopTest(unittest.TestCase):
    """loop=True (the default) cycles messages indefinitely and never
    reports busy — the scene's duration_s controls lifetime instead."""

    def test_loop_default_is_true(self):
        ov = _make_overlay()
        self.assertTrue(ov.loop)

    def test_loop_never_reports_busy(self):
        ov = _make_overlay(messages=[
            {"text": "A"},
            {"text": "B"},
        ])
        ov.setup(api=MagicMock(), scene=MagicMock())
        self.assertFalse(ov.is_busy())
        for t in np.arange(0.0, 10.0, 0.05):
            ov.compose(_make_buffers(), MagicMock(), float(t))
            self.assertFalse(ov.is_busy())

    def test_loop_cycles_back_to_first_message(self):
        ov = _make_overlay(messages=[
            {"text": "A"},
            {"text": "B"},
        ], speed_cells_per_s=400.0, inter_message_pause_s=0.0)
        ov.setup(api=MagicMock(), scene=MagicMock())
        seen_indices: list[int] = []
        for t in np.arange(0.0, 15.0, 0.05):
            ov.compose(_make_buffers(), MagicMock(), float(t))
            seen_indices.append(ov._msg_idx)
        self.assertIn(1, seen_indices)
        first_one = seen_indices.index(1)
        self.assertIn(0, seen_indices[first_one:],
                      "loop=True must wrap _msg_idx back to 0")
        for idx in seen_indices:
            self.assertGreaterEqual(idx, 0)
            self.assertLess(idx, 2)


if __name__ == "__main__":
    unittest.main()
