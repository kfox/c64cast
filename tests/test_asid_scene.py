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


if __name__ == "__main__":
    unittest.main()
