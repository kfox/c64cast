"""Host-side unit tests for AsidScene (c64cast/asid_scene.py).

These exercise the scene's use of the ASID decoder against the shared FakeAPI:
folding SysEx into the register shadow, the coalesced $D400-$D418 block write,
the hard-restart two-phase emit (first control write ordered before the block),
info-row rendering, PAL/NTSC switching, and teardown silence/restore. No MIDI
hardware and no U64 are touched — `_open_port` is patched out and SysEx is fed
straight through `_handle_sysex`.

Real-hardware behavior (sound out of the SID, the live oscilloscope) is covered
separately by a Tier-2 smoke run against an ASID host, not here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    import mido as _mido

    mido: Any = _mido
    HAVE_MIDI = True
except ImportError:
    mido = None
    HAVE_MIDI = False

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast import asid  # noqa: E402
from c64cast.c64 import SID  # noqa: E402
from c64cast.modes import DisplayMode  # noqa: E402


def _reg_msg(values: dict[int, int]) -> tuple[int, ...]:
    """Build a 0x4E payload from {asid_register_id: value} (see test_asid)."""
    mask = [0, 0, 0, 0]
    msb = [0, 0, 0, 0]
    data: list[int] = []
    for reg_id in sorted(values):
        byte_idx, bit = divmod(reg_id, 7)
        mask[byte_idx] |= 1 << bit
        if values[reg_id] & 0x80:
            msb[byte_idx] |= 1 << bit
        data.append(values[reg_id] & 0x7F)
    return (asid.ASID_MANUFACTURER_ID, asid.CMD_REG, *mask, *msb, *data)


@unittest.skipUnless(HAVE_MIDI, "mido not installed (midi extra)")
class AsidSceneTest(unittest.TestCase):
    def _make(self, **kwargs):
        from c64cast.asid_scene import AsidScene

        api = FakeAPI()
        # These tests exercise the coalesced flush path specifically; the FakeAPI
        # reports supports_reu, so the "auto" default would otherwise engage the
        # buffered ring player. The buffered path has its own class below.
        kwargs.setdefault("buffered_player", "off")
        scene = AsidScene(api, None, **kwargs)
        return scene, api

    def _bring_up(self, scene) -> None:
        """Run the bitmap bring-up a full setup() would, minus MIDI/threads."""
        scene._apply_vic_hires_bank()
        scene._alloc_scope_buffers()

    # ---- register shadow + block write --------------------------------------
    def test_frame_folds_into_shadow_and_block_write(self):
        scene, api = self._make()
        # Voice 1 freq lo/hi + control (single write) + master volume.
        scene._handle_sysex(_reg_msg({0: 0x34, 1: 0x12, 22: 0x41, 21: 0x0F}))
        self.assertTrue(scene._pending_flush)
        scene._flush_to_sid()
        block = api.regs[f"{SID.BASE:04X}"]
        self.assertEqual(len(block), 25)
        self.assertEqual(block[0x00], 0x34)
        self.assertEqual(block[0x01], 0x12)
        self.assertEqual(block[0x04], 0x41)  # voice-1 control
        self.assertEqual(block[0x18], 0x0F)  # $D418 master volume
        self.assertFalse(scene._pending_flush)

    def test_hard_restart_writes_first_control_before_block(self):
        scene, api = self._make()
        # Voice 1 hard restart: gate-off (0x08) first, gate-on waveform (0x41) second.
        scene._handle_sysex(_reg_msg({22: 0x08, 25: 0x41}))
        scene._flush_to_sid()
        ctrl_addr = f"{SID.voice_base(0) + SID.OFF_CONTROL:04X}"
        # The individual first-control write must precede the block write.
        op_names = [(op[0], op[1]) for op in api.ops]
        self.assertIn(("write_memory", ctrl_addr), op_names)
        first_idx = op_names.index(("write_memory", ctrl_addr))
        block_idx = op_names.index(("write_regs", f"{SID.BASE:04X}"))
        self.assertLess(first_idx, block_idx)
        # First write lands the gate-off value; block lands the final gate-on.
        self.assertEqual(api.memories[ctrl_addr], "08")
        self.assertEqual(api.regs[f"{SID.BASE:04X}"][0x04], 0x41)
        self.assertEqual(scene._pending_ctrl_first, {})

    def test_foreign_sysex_ignored(self):
        scene, _ = self._make()
        scene._handle_sysex((0x7E, 0x00, 0x01))  # not ASID
        self.assertFalse(scene._pending_flush)

    def test_unsupported_command_warns_once(self):
        scene, _ = self._make()
        with self.assertLogs("c64cast.asid_scene", level="WARNING") as cm:
            scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_OPL, 0x00))
            scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_OPL, 0x00))
        self.assertEqual(len(cm.output), 1)  # warned once, not twice
        self.assertFalse(scene._pending_flush)

    # ---- stream metadata ----------------------------------------------------
    def test_character_display_sets_meta_row(self):
        scene, _ = self._make()
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_CHARS, *map(ord, "NOW PLAYING")))
        self.assertEqual(scene._status_text, "NOW PLAYING")
        self.assertTrue(scene._dirty)
        self.assertIn("NOW PLAYING", scene._build_meta_line())

    def test_start_updates_title(self):
        scene, _ = self._make()
        self.assertIn("READY", scene._build_title_line())
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_START))
        self.assertTrue(scene._playing)
        self.assertIn("PLAYING", scene._build_title_line())

    def test_speed_switches_emulator_clock(self):
        scene, _ = self._make(system="NTSC")
        from c64cast.c64 import CLOCK_PAL

        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x00))  # PAL
        self.assertEqual(scene.system, "PAL")
        self.assertEqual(scene.emulator.clock, CLOCK_PAL)

    # ---- config validation + lifecycle --------------------------------------
    def test_validate_asid_returns_bitmap_mode(self):
        from c64cast.config import SceneCfg, _validate_asid

        # AsidScene is bitmap-only: the validator synthesises a hires mode so
        # overlay-compat rejects PETSCII overlays (as on a waveform scene).
        mode = _validate_asid(SceneCfg(type="asid"))
        self.assertIsInstance(mode, DisplayMode)

    def test_validate_scene_cfg_accepts_asid(self):
        from c64cast.config import Config, SceneCfg, validate_scene_cfg

        # Full dispatch path: an asid scene validates without error.
        validate_scene_cfg(SceneCfg(type="asid"), Config(), audio_enabled=False)

    def test_teardown_silences_and_restores(self):
        scene, api = self._make()
        self._bring_up(scene)
        scene.teardown()
        self.assertIn("SILENCE", api.regs)  # SID silenced
        self.assertIn("DD00", api.memories)  # VIC bank restored

    # ---- multi-SID ----------------------------------------------------------
    def _make_multi(self, sockets=None, **kwargs):
        """A scene on a config-capable (Ultimate-like) backend. `sockets` seeds
        the detected-socket category, e.g. {"SID Detected Socket 1": "6581"}."""
        from c64cast.asid_scene import AsidScene
        from c64cast.backend import HardwareProfile

        api = FakeAPI()
        api.profile = HardwareProfile(name="Fake", family="fake", supports_config=True)
        if sockets:
            from c64cast.asid_sidmap import CAT_SOCKETS

            api.config_store[CAT_SOCKETS] = dict(sockets)
        kwargs.setdefault("buffered_player", "off")  # coalesced-path multi-SID tests
        scene = AsidScene(api, None, **kwargs)
        return scene, api

    def _multi_msg(self, chip_index: int, values: dict[int, int]) -> tuple[int, ...]:
        cmd = asid.CMD_MULTI_SID_LO + (chip_index - 1)
        return (asid.ASID_MANUFACTURER_ID, cmd, *_reg_msg(values)[2:])

    def test_multi_sid_disabled_without_config_api(self):
        # Default FakeAPI has supports_config=False → multi-SID inactive.
        scene, _ = self._make()
        self.assertFalse(scene._multi_sid)
        with self.assertLogs("c64cast.asid_scene", level="WARNING"):
            scene._handle_sysex(self._multi_msg(1, {0: 0x11}))
        # Downmixed to the primary shadow (chip 0), not chip 1.
        self.assertEqual(scene._sid_shadows[0][0x00], 0x11)
        self.assertEqual(scene._sid_shadows[1][0x00], 0x00)

    def test_multi_sid_routes_and_reconfigures(self):
        scene, api = self._make_multi()
        self._bring_up(scene)
        # A SID2 (chip 1) frame arrives.
        scene._handle_sysex(self._multi_msg(1, {0: 0x22, 21: 0x0F}))
        self.assertEqual(scene._max_chip_seen, 1)
        # process_frame grows the map on the main thread.
        scene.process_frame(0.0)
        self.assertEqual(scene._active_chips, 2)
        self.assertEqual(scene._n_windows, 2)
        # The U64 address map was configured live.
        self.assertTrue(any(cat == "SID Addressing" for cat, _, _ in api.config_puts))
        # Chip 1 flushes to its own (non-$D400) address.
        scene._flush_to_sid()
        chip1_addr = f"{scene._chip_addresses[1]:04X}"
        self.assertIn(chip1_addr, api.regs)
        self.assertNotEqual(chip1_addr, f"{SID.BASE:04X}")
        self.assertEqual(api.regs[chip1_addr][0x00], 0x22)

    def test_multi_sid_prefers_physical_socket(self):
        from c64cast.sid_hw_config import detect_sockets

        scene, _ = self._make_multi(sockets={"SID Detected Socket 1": "6581"})
        scene._socket_present = detect_sockets(scene.api)
        self.assertEqual(scene._socket_present, (True, False))
        scene._reconfigure_chips(2)
        # Chip 0 → the physical socket at $D400; chip 1 → an UltiSID above it.
        self.assertEqual(scene._chip_addresses[0], SID.BASE)
        self.assertGreater(scene._chip_addresses[1], SID.BASE)

    def test_multi_sid_teardown_restores_config(self):
        from c64cast.asid_sidmap import CAT_ADDRESSING

        scene, api = self._make_multi()
        # Seed a prior addressing value so restore has something to write back.
        api.config_store[CAT_ADDRESSING] = {"UltiSID Range Split": "Off"}
        self._bring_up(scene)
        scene._reconfigure_chips(3)
        api.config_puts.clear()
        scene.teardown()
        # The snapshotted split value is restored.
        self.assertIn((CAT_ADDRESSING, "UltiSID Range Split", "Off"), api.config_puts)

    def test_setup_opens_port_and_starts_threads(self):
        scene, api = self._make()
        with mock.patch.object(scene, "_open_port"):
            scene._midi_port = None  # _open_port patched; reader exits immediately
            scene.setup()
        try:
            self.assertGreaterEqual(api.cache_invalidations, 1)
            self.assertIsNotNone(scene._reader_thread)
        finally:
            scene.teardown()


@unittest.skipUnless(HAVE_MIDI, "mido not installed (midi extra)")
class AsidBufferedPlayerTest(unittest.TestCase):
    """The buffered ring-player path: frame grouping, serialization to the ring
    player, and REU gating. The player's own ring math is tested in
    test_asid_player; here we assert the scene wires frames into it."""

    def _make(self, **kwargs):
        from c64cast.asid_scene import AsidScene

        api = FakeAPI()  # supports_reu=True → "auto" engages the buffered player
        scene = AsidScene(api, None, buffered_player="auto", **kwargs)
        return scene, api

    def test_auto_engages_when_reu_present(self):
        scene, _ = self._make()
        self.assertTrue(scene._use_buffered_player)
        self.assertIsNotNone(scene._player)

    def test_off_forces_coalesced(self):
        from c64cast.asid_scene import AsidScene

        scene = AsidScene(FakeAPI(), None, buffered_player="off")
        self.assertFalse(scene._use_buffered_player)
        self.assertIsNone(scene._player)

    def test_on_without_reu_warns_and_falls_back(self):
        from c64cast.asid_scene import AsidScene
        from c64cast.backend import HardwareProfile

        api = FakeAPI()
        api.profile = HardwareProfile(name="Fake", family="fake", supports_reu=False)
        with self.assertLogs("c64cast.asid_scene", level="WARNING"):
            scene = AsidScene(api, None, buffered_player="on")
        self.assertFalse(scene._use_buffered_player)

    def test_frame_boundary_pushes_a_slot(self):
        scene, _ = self._make()
        player = scene._player
        assert player is not None
        pushed: list[bytes] = []
        # Replace the real player with a stub capturing push_frame.
        player.push_frame = pushed.append  # type: ignore[method-assign]
        # First 0x4E starts a frame; the second 0x4E flushes the first.
        scene._handle_sysex(_reg_msg({0: 0x34, 1: 0x12, 22: 0x41, 21: 0x0F}))
        self.assertTrue(scene._frame_has_data)
        self.assertEqual(pushed, [])  # not emitted until the boundary
        scene._handle_sysex(_reg_msg({0: 0x40}))
        self.assertEqual(len(pushed), 1)
        self.assertEqual(len(pushed[0]), player.slot_size)
        # The emitted slot carries the first frame's ops (n_ops > 0).
        self.assertGreater(pushed[0][0], 0)

    def test_stop_boundary_flushes_partial_frame(self):
        scene, _ = self._make()
        player = scene._player
        assert player is not None
        pushed: list[bytes] = []
        player.push_frame = pushed.append  # type: ignore[method-assign]
        scene._handle_sysex(_reg_msg({0: 0x34, 22: 0x41}))
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_STOP))
        self.assertEqual(len(pushed), 1)
        self.assertFalse(scene._frame_has_data)

    def test_speed_message_retunes_player(self):
        scene, _ = self._make()
        player = scene._player
        assert player is not None
        rates: list[float] = []
        player.set_frame_rate = rates.append  # type: ignore[method-assign]
        player._armed = True  # so _apply_speed forwards
        # NTSC, multiplier 4 (data0 bits 1-4 = 3 → ×4).
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, (3 << 1) | 0x01))
        self.assertTrue(rates)
        self.assertAlmostEqual(rates[-1], 60.0 * 4, delta=1.0)

    def test_recipe_stored_from_timing_message(self):
        scene, _ = self._make()
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_TIMING, 0x01, 0x00, 0x00, 0x00))
        self.assertEqual(scene._recipe, [(1, 0), (0, 0)])

    def test_wants_reu_flags_buffered_asid(self):
        from c64cast.config import Config, SceneCfg
        from c64cast.doctor import _wants_reu

        cfg = Config()
        cfg.scenes = [SceneCfg(type="asid", asid_buffered_player="on")]
        wants, reasons = _wants_reu(cfg)
        self.assertTrue(wants)
        self.assertTrue(any("asid" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
