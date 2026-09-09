"""Host-side unit tests for AsidScene (c64cast/sid/asid_scene.py).

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
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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
from _fakes import FakeAPI, quiet_logging  # noqa: E402

from c64cast.hw.c64 import SID  # noqa: E402
from c64cast.sid import asid  # noqa: E402
from c64cast.sid.asid_player import frame_cycle_cost  # noqa: E402
from c64cast.video.modes import DisplayMode  # noqa: E402


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


class _FakeClock:
    """Drives the scene's 0x31 retune window without sleeping on it, so nothing
    here depends on how long the test host takes to run a loop."""

    def __init__(self, scene, now: float = 1000.0):
        self.now = now
        scene._monotonic = lambda: self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _stub_port(scene):
    """Patch `_open_port` with a stub MIDI port that polls empty, so setup()
    can run its real lifecycle without MIDI hardware."""
    port = mock.MagicMock()
    port.poll.return_value = None
    return mock.patch.object(
        scene, "_open_port", side_effect=lambda: setattr(scene, "_midi_port", port)
    )


@unittest.skipUnless(HAVE_MIDI, "mido not installed (midi extra)")
class AsidSceneTest(unittest.TestCase):
    def _make(self, **kwargs):
        from c64cast.sid.asid_scene import AsidScene

        api = FakeAPI()
        # These tests exercise the coalesced flush path specifically; the FakeAPI
        # reports supports_reu, so the "auto" default would otherwise engage the
        # buffered ring player. The buffered path has its own class below.
        kwargs.setdefault("buffered_player", "off")
        scene = AsidScene(api, None, **kwargs)
        return scene, api

    def test_two_scenes_do_not_share_their_wire_report_budgets(self):
        # The production half of the per-stream rule: one AsidScene is one MIDI
        # input port, so two of them in a process — an ensemble, one per system
        # — must not share a report budget, or the first system to be flooded
        # takes the only report and the second never reports its own first
        # occurrence. The decoder and the packer are free functions, so the
        # scene is what owns the two budgets; see c64cast/_wire_log.py.
        a, _ = self._make(port="A")
        b, _ = self._make(port="B")
        self.assertIsNot(a._recipe_log, b._recipe_log)
        self.assertIsNot(a._truncation_log, b._truncation_log)

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
        with self.assertLogs("c64cast.sid.asid_scene", level="WARNING") as cm:
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
        from c64cast.hw.c64 import CLOCK_PAL

        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x00))  # PAL
        self.assertEqual(scene.system, "PAL")
        self.assertEqual(scene.emulator.clock, CLOCK_PAL)

    # ---- config validation + lifecycle --------------------------------------
    def test_validate_asid_returns_bitmap_mode(self):
        from c64cast.app.config import Config, SceneCfg
        from c64cast.app.scene_factory import _validate_asid

        # AsidScene is bitmap-only: the validator synthesizes a hires mode so
        # overlay-compat rejects PETSCII overlays (as on a waveform scene).
        mode = _validate_asid(SceneCfg(type="asid"), Config())
        self.assertIsInstance(mode, DisplayMode)

    def test_validate_scene_cfg_accepts_asid(self):
        from c64cast.app.config import Config, SceneCfg
        from c64cast.app.scene_factory import validate_scene_cfg

        # Full dispatch path: an asid scene validates without error.
        validate_scene_cfg(SceneCfg(type="asid"), Config(), audio_enabled=False)

    def test_teardown_silences_and_restores(self):
        scene, api = self._make()
        self._bring_up(scene)
        scene.teardown()
        self.assertIn("SILENCE", api.regs)  # SID silenced
        self.assertIn("DD00", api.memories)  # VIC bank restored

    def test_a_failing_silence_does_not_starve_the_config_and_display_restores(self):
        def link_down(*args, **kwargs) -> None:
            raise RuntimeError("DMA link down")

        scene, api = self._make()
        self._bring_up(scene)
        self.assertEqual(api.memories["D018"], "18")
        api.silence_sid = link_down  # type: ignore[method-assign]
        with self.assertLogs("c64cast.sid.asid_scene", level="ERROR"):
            scene.teardown()
        # $14 is the char-mode byte; the scope left the bitmap layout ($18).
        self.assertEqual(api.memories["D018"], "14")

    def test_teardown_leaves_d018_on_the_char_mode_default(self):
        # The scope ran in hires ($18 = bitmap at bank+$2000). Teardown claims
        # to hand the next scene the char-mode default, so it must write the
        # value every char-mode engage in the tree writes, not its own.
        scene, api = self._make()
        self._bring_up(scene)
        self.assertEqual(api.memories["D018"], "18")
        scene.teardown()
        # The literal is the point: comparing against D018_CHAR_DEFAULT compares
        # teardown's write to the constant it wrote it from, which stayed green
        # with the constant set to the hires $18. $14 is the char-mode byte every
        # char-mode engage in the tree writes (matrix at bank+$0400, char gen at
        # +$1000, bitmap bit clear); test_voice_scope pins the constant to it.
        self.assertEqual(api.memories["D018"], "14")

    # ---- multi-SID ----------------------------------------------------------
    def _make_multi(self, sockets=None, **kwargs):
        """A scene on a config-capable (Ultimate-like) backend. `sockets` seeds
        the detected-socket category, e.g. {"SID Detected Socket 1": "6581"}."""
        from c64cast.hw.backend import HardwareProfile
        from c64cast.sid.asid_scene import AsidScene

        api = FakeAPI()
        api.profile = HardwareProfile(
            name="Fake", family="fake", supports_config=True, supports_sid_config=True
        )
        if sockets:
            from c64cast.sid.asid_sidmap import CAT_SOCKETS

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
        with self.assertLogs("c64cast.sid.asid_scene", level="WARNING"):
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
        from c64cast.sid.sid_hw_config import detect_sockets

        scene, _ = self._make_multi(sockets={"SID Detected Socket 1": "6581"})
        scene._socket_present = detect_sockets(scene.api)
        self.assertEqual(scene._socket_present, (True, False))
        scene._reconfigure_chips(2)
        # Chip 0 → the physical socket at $D400; chip 1 → an UltiSID above it.
        self.assertEqual(scene._chip_addresses[0], SID.BASE)
        self.assertGreater(scene._chip_addresses[1], SID.BASE)

    def test_multi_sid_teardown_restores_config(self):
        from c64cast.sid.asid_sidmap import CAT_ADDRESSING

        scene, api = self._make_multi()
        # Seed a prior addressing value so restore has something to write back.
        api.config_store[CAT_ADDRESSING] = {"UltiSID Range Split": "Off"}
        self._bring_up(scene)
        # 3 chips outrun the 2 pannable UltiSID cores, which warns; the pan
        # fallback has its own test, this one is about the teardown restore.
        with quiet_logging():
            scene._reconfigure_chips(3)
        api.config_puts.clear()
        scene.teardown()
        # The snapshotted split value is restored.
        self.assertIn((CAT_ADDRESSING, "UltiSID Range Split", "Off"), api.config_puts)

    def test_addressing_baseline_survives_the_setup_mixer_fold(self):
        """The whole lifecycle, not just the remap: setup()'s mixer pass folds
        originals into the same first-call-wins session, so the addressing
        baseline has to be taken before it — otherwise the remap's own
        snapshot() no-ops and a remote 0x50 frame rewrites the U64's SID
        addressing with nothing to put back."""
        from c64cast.sid.asid_sidmap import CAT_ADDRESSING

        scene, api = self._make_multi()
        # A stock-shaped machine: a real addressing baseline, and a mixer that
        # is NOT already at the pan/volume target (so the mixer pass has
        # originals to fold, which is the precondition for the bug).
        api.config_store[CAT_ADDRESSING] = {
            "UltiSID 1 Address": "$D400",
            "UltiSID Range Split": "Off",
            "Auto Address Mirroring": "Enabled",
        }
        api.config_store["Audio Mixer"] = {"Pan UltiSID 1": "Left 3", "Vol UltiSID 1": "-6 dB"}
        with _stub_port(scene), quiet_logging():
            scene.setup()
            scene._reconfigure_chips(2)
        saved = scene._sid_session.saved or {}
        self.assertIn((CAT_ADDRESSING, "Auto Address Mirroring"), saved)
        api.config_puts.clear()
        with quiet_logging():
            scene.teardown()
        self.assertIn(
            (CAT_ADDRESSING, "Auto Address Mirroring", "Enabled"),
            api.config_puts,
        )

    def test_a_second_lap_gets_its_own_baseline_after_a_failing_teardown(self):
        """The starved restore compounded across laps, and the guarded steps are
        what stop it.

        `SidHwSession.snapshot()` is first-call-wins and only `restore()` clears
        the recorded set, so a teardown that never reached the config restore
        left lap 2's `snapshot()` a no-op — lap 2 then had no baseline of its
        own, and every later lap inherited lap 1's. Failing the silence ahead of
        the restore is the cheapest way to drive that path.
        """
        from c64cast.sid.asid_sidmap import CAT_ADDRESSING

        scene, api = self._make_multi()
        api.config_store[CAT_ADDRESSING] = {"UltiSID Range Split": "Off"}
        with _stub_port(scene), quiet_logging():
            scene.setup()
            scene._reconfigure_chips(3)
        with mock.patch.object(api, "silence_sid", side_effect=RuntimeError("DMA link down")):
            with self.assertLogs("c64cast.sid.asid_scene", level="ERROR"):
                scene.teardown()
        self.assertIsNone(scene._sid_session.saved, "the config restore was starved")

        # Lap 2, against a machine the user has since reconfigured. The restore
        # has to put *this* lap's value back, not the one lap 1 recorded.
        api.config_store[CAT_ADDRESSING] = {"UltiSID Range Split": "On"}
        with _stub_port(scene), quiet_logging():
            scene.setup()
            scene._reconfigure_chips(3)
        api.config_puts.clear()
        with quiet_logging():
            scene.teardown()
        self.assertIn((CAT_ADDRESSING, "UltiSID Range Split", "On"), api.config_puts)

    def test_out_of_range_chip_index_does_not_drive_a_remap(self):
        """`_chip_for` downmixes an index past the cap to slot 0, so the growth
        request must come from that mapped slot: growing on the raw wire index
        maps chips no data can ever reach."""
        scene, api = self._make_multi(max_sids=2)
        self._bring_up(scene)
        with self.assertLogs("c64cast.sid.asid_scene", level="WARNING"):
            scene._handle_sysex(self._multi_msg(5, {0: 0x11}))  # chip 5, cap 2
        self.assertEqual(scene._max_chip_seen, 0)
        scene.process_frame(0.0)
        self.assertEqual(scene._active_chips, 1)
        self.assertEqual(scene._n_windows, 1)
        self.assertFalse(any(cat == "SID Addressing" for cat, _, _ in api.config_puts))
        # The data still landed, downmixed onto the primary chip.
        self.assertEqual(scene._sid_shadows[0][0x00], 0x11)

    def test_in_range_chip_index_still_grows_the_map(self):
        # Same shape, one field varied: an index inside the cap must remap.
        scene, api = self._make_multi(max_sids=2)
        self._bring_up(scene)
        scene._handle_sysex(self._multi_msg(1, {0: 0x11}))
        self.assertEqual(scene._max_chip_seen, 1)
        scene.process_frame(0.0)
        self.assertEqual(scene._active_chips, 2)
        self.assertTrue(any(cat == "SID Addressing" for cat, _, _ in api.config_puts))

    def test_failed_remap_retries_instead_of_killing_the_scene(self):
        """A link hiccup inside the remap must not leave `_active_chips` ahead
        of the scope's window count (every later frame would then IndexError,
        and the playlist retires a crashing scene for good)."""
        scene, api = self._make_multi(buffered_player="auto")
        self._bring_up(scene)
        scene._handle_sysex(self._multi_msg(1, {0: 0x22}))
        assert scene._player is not None
        # The successful retry re-inits the ring player, which starts its writer
        # thread — stop it however this test exits.
        self.addCleanup(scene._player.stop)
        failing = mock.patch.object(scene._player, "reinit", side_effect=OSError("DMA link hiccup"))
        with failing, self.assertLogs("c64cast.sid.asid_scene", level="WARNING") as cm:
            scene.process_frame(0.0)
        self.assertIn("retrying on the next frame", cm.output[0])
        self.assertEqual(scene._active_chips, 1)
        self.assertEqual(scene._n_windows, 1)
        # Later frames render rather than raise, and the remap is retried.
        with quiet_logging():
            scene.process_frame(0.1)
        self.assertEqual(scene._active_chips, 2)
        self.assertEqual(scene._n_windows, 2)

    def test_reactivation_re_derives_the_sid_map(self):
        """Playlists reuse scene instances. Lap 2 must re-apply the address map
        the lap-1 teardown just restored, which means the multi-SID shape has
        to be back to single-chip when setup() returns."""
        scene, api = self._make_multi()
        with _stub_port(scene), quiet_logging():
            scene.setup()
            scene._handle_sysex(self._multi_msg(1, {0: 0x22}))
            scene.process_frame(0.0)
            self.assertEqual(scene._active_chips, 2)
            scene.teardown()
            scene.setup()
        try:
            self.assertEqual(scene._active_chips, 1)
            self.assertEqual(scene._n_windows, 1)
            self.assertEqual(scene._max_chip_seen, 0)
            self.assertEqual(scene._chip_addresses, [SID.BASE])
            api.config_puts.clear()
            with quiet_logging():
                scene._handle_sysex(self._multi_msg(1, {0: 0x22}))
                scene.process_frame(0.1)
            self.assertTrue(any(cat == "SID Addressing" for cat, _, _ in api.config_puts))
        finally:
            with quiet_logging():
                scene.teardown()

    def test_reactivation_forgets_the_previous_tunes_register_image(self):
        """A flush writes the whole 25-byte image, so lap 1's ADSR, pulse widths
        and filter settings would otherwise ride along with the first lap-2 frame
        that touches any register at all. Teardown silenced the chips, so zero is
        what the hardware holds when the next lap starts."""
        scene, api = self._make()
        scene._handle_sysex(_reg_msg({4: 0x7F, 21: 0x0F}))  # voice-1 AD + master vol
        scene._flush_to_sid()
        with _stub_port(scene), quiet_logging():
            scene.setup()
        try:
            scene._handle_sysex(_reg_msg({0: 0x34}))  # lap 2 writes one register
            scene._flush_to_sid()
            block = api.regs[f"{SID.BASE:04X}"]
            self.assertEqual(block[0x00], 0x34)
            self.assertEqual(block[0x05], 0x00)  # lap 1's attack/decay is gone
            self.assertEqual(block[0x18], 0x00)  # ...and its master volume
        finally:
            with quiet_logging():
                scene.teardown()

    def test_setup_opens_port_and_starts_threads(self):
        scene, api = self._make()
        # Avoid touching real MIDI hardware: a stub port that never yields a
        # message keeps the reader loop alive so is_running() is observable.
        with _stub_port(scene):
            scene.setup()
        try:
            self.assertGreaterEqual(api.cache_invalidations, 1)
            self.assertTrue(scene._reader_poll.is_running())
        finally:
            scene.teardown()

    # ---- reader drain --------------------------------------------------------
    def test_reader_drain_is_bounded_so_the_flush_is_always_reached(self):
        """A backlog arriving faster than it is retired must not starve the
        coalesced flush (the SID would hold its last state and keep sounding)
        or the stop check that ends teardown."""
        from c64cast._midi import MAX_MSGS_PER_DRAIN

        scene, _ = self._make()
        stop = threading.Event()
        polls = {"n": 0}
        payload = _reg_msg({0: 0x34, 22: 0x41})

        def poll():
            polls["n"] += 1
            # A large but finite backlog: an unbounded drain consumes it all
            # before reaching the flush, a bounded one stops at the cap.
            return SimpleNamespace(type="sysex", data=payload) if polls["n"] <= 5000 else None

        polls_at_first_flush: list[int] = []
        real_flush = scene._flush_to_sid

        def flush():
            real_flush()
            polls_at_first_flush.append(polls["n"])
            stop.set()

        scene._midi_port = SimpleNamespace(
            poll=poll,
            iter_pending=lambda: iter(poll, None),
        )
        with mock.patch.object(scene, "_flush_to_sid", side_effect=flush):
            scene._reader(stop)
        self.assertEqual(len(polls_at_first_flush), 1)
        self.assertLessEqual(polls_at_first_flush[0], MAX_MSGS_PER_DRAIN)

    def test_reader_releases_a_pass_of_expensive_messages_and_still_flushes(self):
        """The count bound alone does not protect the flush: it bounds messages
        while the wire picks the work per message. A WARNING rendered by the
        default terminal handler costs ~322 us, so 64 of them inside one pass is
        20.6 ms — the flush and the stop check both live after the drain."""
        from c64cast import _midi

        scene, _ = self._make()
        stop = threading.Event()
        polls = {"n": 0}
        payload = _reg_msg({0: 0x34, 22: 0x41})

        def poll():
            polls["n"] += 1
            return SimpleNamespace(type="sysex", data=payload) if polls["n"] <= 5000 else None

        # Each message costs a third of the drain's work budget — the same shape
        # as a Rich-rendered WARNING, an order of magnitude cheaper.
        clock = {"now": 1000.0}

        def monotonic():
            clock["now"] += _midi.MAX_DRAIN_WORK_S / 3
            return clock["now"]

        polls_at_first_flush: list[int] = []
        real_flush = scene._flush_to_sid

        def flush():
            real_flush()
            polls_at_first_flush.append(polls["n"])
            stop.set()

        scene._midi_port = SimpleNamespace(poll=poll, iter_pending=lambda: iter(poll, None))
        with mock.patch.object(_midi, "_monotonic", monotonic):
            with mock.patch.object(scene, "_flush_to_sid", side_effect=flush):
                scene._reader(stop)
        # The flush ran, with the backlog still deep and long before the count
        # bound would have released the pass on its own.
        self.assertEqual(len(polls_at_first_flush), 1)
        self.assertLessEqual(polls_at_first_flush[0], 8)
        self.assertLess(polls["n"], 5000)

    def test_reader_stops_mid_drain_when_the_stop_event_is_set(self):
        scene, _ = self._make()
        stop = threading.Event()
        seen: list[int] = []
        payload = _reg_msg({0: 0x34})

        def poll():
            seen.append(1)
            if len(seen) == 3:
                stop.set()  # teardown lands mid-backlog
            # A finite backlog, so a drain that ignores `stop` still terminates
            # (and is then visible as an over-long read rather than a hang).
            return SimpleNamespace(type="sysex", data=payload) if len(seen) <= 200 else None

        scene._midi_port = SimpleNamespace(poll=poll, iter_pending=lambda: iter(poll, None))
        scene._reader(stop)
        self.assertEqual(len(seen), 3)


@unittest.skipUnless(HAVE_MIDI, "mido not installed (midi extra)")
class AsidBufferedPlayerTest(unittest.TestCase):
    """The buffered ring-player path: frame grouping, serialization to the ring
    player, and REU gating. The player's own ring math is tested in
    test_asid_player; here we assert the scene wires frames into it."""

    def _make(self, **kwargs):
        from c64cast.sid.asid_scene import AsidScene

        api = FakeAPI()  # supports_reu=True → "auto" engages the buffered player
        scene = AsidScene(api, None, buffered_player="auto", **kwargs)
        return scene, api

    def test_auto_engages_when_reu_present(self):
        scene, _ = self._make()
        self.assertTrue(scene._use_buffered_player)
        self.assertIsNotNone(scene._player)

    def test_off_forces_coalesced(self):
        from c64cast.sid.asid_scene import AsidScene

        scene = AsidScene(FakeAPI(), None, buffered_player="off")
        self.assertFalse(scene._use_buffered_player)
        self.assertIsNone(scene._player)

    def test_on_without_reu_warns_and_falls_back(self):
        from c64cast.hw.backend import HardwareProfile
        from c64cast.sid.asid_scene import AsidScene

        api = FakeAPI()
        api.profile = HardwareProfile(name="Fake", family="fake", supports_reu=False)
        with self.assertLogs("c64cast.sid.asid_scene", level="WARNING"):
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

    def _capture_rates(self, scene) -> list[float]:
        player = scene._player
        assert player is not None
        rates: list[float] = []
        player.set_frame_rate = rates.append  # type: ignore[method-assign]
        return rates

    def test_speed_message_retunes_player_before_it_ever_arms(self):
        """`_apply_speed` has no `_armed` gate on purpose: a 0x31 almost always
        arrives at stream start, before the prebuffer fills, and dropping it
        arms at the wrong cadence and decimates the tune (sid.md 'Symptom 2')."""
        scene, _ = self._make()
        rates = self._capture_rates(scene)
        assert scene._player is not None
        self.assertFalse(scene._player._armed)
        # NTSC, multiplier 4 (data0 bits 1-4 = 3 → ×4).
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, (3 << 1) | 0x01))
        self.assertTrue(rates)
        self.assertAlmostEqual(rates[-1], 60.0 * 4, delta=1.0)
        self.assertAlmostEqual(scene._frame_rate_hz, 60.0 * 4, delta=1.0)

    def test_speed_clamps_a_hostile_frame_delta(self):
        """One 8-byte SysEx asking for a 1 µs frame delta is a 1 MHz consume
        rate; `cia1_latch_for_rate` clamps the latch, not the rate, so it used
        to reach the CIA as a ~511 kHz IRQ storm. The scene hands the derived
        rate to the player's single clamp boundary."""
        from c64cast.sid.asid_player import MAX_FRAME_RATE_HZ

        scene, _ = self._make()
        rates = self._capture_rates(scene)
        with self.assertLogs("c64cast.sid.asid_player", level="WARNING") as cm:
            scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x01, 0x01, 0x00, 0x00))
        self.assertIn("outside the", cm.output[0])
        self.assertEqual(rates, [MAX_FRAME_RATE_HZ])
        # The scene's own accounting agrees with what the player was given.
        self.assertEqual(scene._frame_rate_hz, MAX_FRAME_RATE_HZ)

    def test_speed_at_the_band_edge_is_forwarded_unclamped(self):
        """The same shape with one field varied: a delta landing exactly on the
        ceiling is legitimate and must pass through silently, so the clamp is a
        bound rather than a cap on ordinary multispeed."""
        from c64cast.sid.asid_player import MAX_FRAME_RATE_HZ

        scene, _ = self._make()
        rates = self._capture_rates(scene)
        # frame_delta_us = 1000 → exactly MAX_FRAME_RATE_HZ.
        with self.assertNoLogs("c64cast.sid.asid_player", level="WARNING"):
            scene._handle_sysex(
                (asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x01, 1000 & 0x7F, 1000 >> 7, 0x00)
            )
        self.assertEqual(rates, [MAX_FRAME_RATE_HZ])

    def test_repeated_speed_message_does_not_retune_again(self):
        """Each retune is a blocking CIA write plus a flush() round trip on the
        single shared DMA socket, so an identical 0x31 must cost nothing."""
        from c64cast.sid.asid_scene import _SPEED_RETUNE_INTERVAL_S

        scene, _ = self._make()
        clock = _FakeClock(scene)
        rates = self._capture_rates(scene)
        msg = (asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, (3 << 1) | 0x01)
        for _ in range(5):
            scene._handle_sysex(msg)
        self.assertEqual(len(rates), 1)
        # A genuinely different request gets through too — but when the retune
        # window opens, not the instant it arrives. Deduplicating the argument
        # was the whole throttle once, and alternating two rates defeated it.
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, (1 << 1) | 0x01))
        self.assertEqual(len(rates), 1)
        clock.advance(_SPEED_RETUNE_INTERVAL_S)
        scene._retune_if_due()
        self.assertEqual(len(rates), 2)
        self.assertAlmostEqual(rates[-1], 60.0 * 2, delta=1.0)

    def test_a_speed_flood_cannot_outrun_the_retune_window(self):
        """A sender alternating two in-band rates costs what one repeated rate
        costs. 999 and 1000 Hz are both inside the clamp band, so the argument
        dedupe saw two different requests and not even a warning fired — while
        each surviving 0x31 blocks the MIDI reader on a CIA write plus a flush()
        over the socket the render path shares, backing rtmidi's unbounded input
        queue up behind the drain."""
        from c64cast.sid.asid_scene import _SPEED_RETUNE_INTERVAL_S

        scene, _ = self._make()
        clock = _FakeClock(scene)
        rates = self._capture_rates(scene)
        seconds = 6.0
        messages = 6000  # a 1000 Hz sender, six seconds of stream
        for i in range(messages):
            delta_us = (1000, 1001)[i % 2]
            scene._handle_sysex(
                (
                    asid.ASID_MANUFACTURER_ID,
                    asid.CMD_SPEED,
                    0x01,
                    delta_us & 0x7F,
                    (delta_us >> 7) & 0x7F,
                    0x00,
                )
            )
            clock.advance(seconds / messages)
        self.assertGreater(len(rates), 0)  # legitimate retunes still happen
        self.assertLessEqual(len(rates), int(seconds / _SPEED_RETUNE_INTERVAL_S) + 1)

    def test_a_request_inside_the_window_is_coalesced_not_dropped(self):
        """The window latches the newest request instead of discarding it, so a
        host that changes speed twice in quick succession ends up at the second
        speed rather than the first."""
        from c64cast.sid.asid_scene import _SPEED_RETUNE_INTERVAL_S

        scene, _ = self._make()
        clock = _FakeClock(scene)
        rates = self._capture_rates(scene)
        for multiplier in (2, 4, 8):
            scene._handle_sysex(
                (asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, ((multiplier - 1) << 1) | 0x01)
            )
        self.assertEqual(len(rates), 1)  # only the first crossed the window
        clock.advance(_SPEED_RETUNE_INTERVAL_S)
        scene._retune_if_due()
        self.assertAlmostEqual(rates[-1], 60.0 * 8, delta=1.0)

    def test_the_reader_loop_applies_a_latched_retune(self):
        """A 0x31 that lands inside the window is applied when the window opens,
        and the reader loop is what opens it — otherwise a host that changes
        speed once and goes quiet would stay at the old cadence forever."""
        scene, _ = self._make()
        player = scene._player
        assert player is not None
        stop = threading.Event()
        rates: list[float] = []

        def set_rate(hz):
            rates.append(hz)
            stop.set()

        player.set_frame_rate = set_rate  # type: ignore[method-assign]
        scene._speed_request_hz = 120.0  # what a 0x31 inside the window latched
        polls = {"n": 0}

        def poll():
            polls["n"] += 1
            if polls["n"] > 50:
                stop.set()  # safety net: the loop must not spin forever
            return None

        scene._midi_port = SimpleNamespace(poll=poll, iter_pending=lambda: iter(poll, None))
        scene._reader(stop)
        self.assertEqual(rates, [120.0])

    def test_repeated_out_of_band_speed_message_warns_once(self):
        # The dedupe sits before the clamp, so a flood of identical hostile
        # 0x31s can't turn into a WARNING per message either.
        scene, _ = self._make()
        self._capture_rates(scene)
        msg = (asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x01, 0x01, 0x00, 0x00)
        with self.assertLogs("c64cast.sid.asid_player", level="WARNING") as cm:
            for _ in range(10):
                scene._handle_sysex(msg)
        self.assertEqual(len(cm.output), 1)

    def test_unmapped_chip_keeps_its_deltas_until_the_remap(self):
        """The buffered path serializes deltas, so a chip's first frame — which
        arrives before process_frame has mapped it — must be carried forward,
        not cleared. Clearing loses that chip's initial ADSR / pulse-width /
        control setup for good; the host never re-sends it."""
        from c64cast.hw.backend import HardwareProfile
        from c64cast.sid.asid_scene import AsidScene

        api = FakeAPI()
        api.profile = HardwareProfile(
            name="Fake", family="fake", supports_config=True, supports_sid_config=True
        )
        scene = AsidScene(api, None, buffered_player="auto")
        scene._apply_vic_hires_bank()
        scene._alloc_scope_buffers()
        player = scene._player
        assert player is not None
        # The remap re-inits the ring player, which starts its writer thread.
        self.addCleanup(player.stop)
        pushed: list[bytes] = []
        player.push_frame = pushed.append  # type: ignore[method-assign]

        # Chip 1's setup frame, then a chip-0 message closing the frame — chip 1
        # is not mapped yet, so nothing of its can be serialized.
        scene._handle_sysex(self._multi_msg(1, {4: 0x11, 5: 0x22, 22: 0x41}))
        scene._handle_sysex(_reg_msg({0: 0x34}))  # frame boundary: emits, drops nothing
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][0], 0)  # no ops — chip 1 had no address yet
        self.assertEqual(scene._frame_regs.get(1), {0x05: 0x11, 0x06: 0x22, 0x04: 0x41})

        # The remap lands, and the next boundary carries chip 1's held deltas.
        with quiet_logging():
            scene.process_frame(0.0)
        self.assertEqual(scene._active_chips, 2)
        pushed.clear()
        scene._handle_sysex(_reg_msg({0: 0x40}))
        chip1_base = scene._chip_addresses[1]
        targets = self._slot_addresses(pushed[0])
        self.assertIn(chip1_base + 0x05, targets)  # voice-1 attack/decay
        self.assertIn(chip1_base + 0x04, targets)  # voice-1 control
        # Chip 1's accumulator is only cleared once it has actually been sent.
        self.assertNotIn(1, scene._frame_regs)

    @staticmethod
    def _slot_addresses(slot: bytes) -> list[int]:
        """The op target addresses in a packed ring slot ([n_ops][lo hi val wait]*)."""
        n_ops = slot[0]
        return [slot[1 + i * 4] | (slot[2 + i * 4] << 8) for i in range(n_ops)]

    @staticmethod
    def _slot_ops(slot: bytes) -> list[tuple[int, int, int]]:
        """Every op in a packed ring slot as (addr, value, wait_units)."""
        return [
            (
                slot[1 + i * 4] | (slot[2 + i * 4] << 8),
                slot[3 + i * 4],
                slot[4 + i * 4],
            )
            for i in range(slot[0])
        ]

    def _maximal_recipe_frame(self, scene) -> None:
        """Feed a spec-legal 28-pair `0x30` at the maximum wait, then a full
        register frame with a hard restart on all three voices, then the chip-0
        message that closes it."""
        recipe = tuple(b for rid in range(28) for b in ((rid & 0x3F) | 0x40, 0x7F))
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_TIMING, *recipe))
        values = dict.fromkeys(range(22), 0x11)
        values.update({22: 0x08, 23: 0x08, 24: 0x08, 25: 0x41, 26: 0x41, 27: 0x41})
        scene._handle_sysex(_reg_msg(values))
        scene._handle_sysex(_reg_msg({0: 0x40}))

    def test_a_maximal_recipe_frame_is_held_to_what_one_tick_can_execute(self):
        """The op *count* is bounded at the decoder, but the `0x30` wait column
        is wire-supplied and was not: 28 maximum waits are most of a 60 Hz NTSC
        frame for a single chip. An overrunning frame does not queue — the CIA
        fires again before the handler returns, so the 6510 never leaves the ASID
        IRQ and the kernal tail stops for as long as the stream keeps it up."""
        scene, _ = self._make()
        player = scene._player
        assert player is not None
        player._rate = 120.0  # an ordinary 2x multispeed
        pushed: list[bytes] = []
        player.push_frame = pushed.append  # type: ignore[method-assign]
        with self.assertLogs("c64cast.sid.asid_scene", level="WARNING") as cm:
            self._maximal_recipe_frame(scene)
        self.assertIn("consume tick", cm.output[0])
        ops = self._slot_ops(pushed[0])
        self.assertEqual(len(ops), 28)  # every register write still reaches the SID
        self.assertLessEqual(frame_cycle_cost(ops), player.frame_cycle_budget())

    def test_a_frame_inside_the_budget_keeps_its_recipe_waits(self):
        """The same frame with one field varied — the consume rate the waits are
        measured against. At single speed it fits, so nothing is scaled and
        nothing is logged: the budget is a ceiling, not a cap on ordinary
        multispeed content."""
        scene, _ = self._make()
        player = scene._player
        assert player is not None
        player._rate = 60.0
        pushed: list[bytes] = []
        player.push_frame = pushed.append  # type: ignore[method-assign]
        with self.assertNoLogs("c64cast.sid.asid_scene", level="WARNING"):
            self._maximal_recipe_frame(scene)
        waits = {wait for _a, _v, wait in self._slot_ops(pushed[0])}
        # 51 units, literal: the wire maximum 255 cycles at DELAY_CYCLES_PER_UNIT.
        # Computing it from the converter under test is what let a 3.4x error
        # in that constant pass (tests/test_asid_player.py CostModelConstantsTest).
        self.assertEqual(waits, {51})

    def _multi_msg(self, chip_index: int, values: dict[int, int]) -> tuple[int, ...]:
        cmd = asid.CMD_MULTI_SID_LO + (chip_index - 1)
        return (asid.ASID_MANUFACTURER_ID, cmd, *_reg_msg(values)[2:])

    def test_recipe_stored_from_timing_message(self):
        scene, _ = self._make()
        scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_TIMING, 0x01, 0x00, 0x00, 0x00))
        self.assertEqual(scene._recipe, [(1, 0), (0, 0)])

    def test_a_failing_player_stop_does_not_starve_the_kernal_irq_restore(self):
        # teardown's own comment argues the scene must not delegate the
        # quiescence promise to the player's bookkeeping. A stop() that raises
        # is that argument's other half: the restore has to be a step of its
        # own, not a statement sequenced behind the stop that can fail.
        from c64cast.hw.c64 import KERNAL

        def wedged() -> None:
            raise RuntimeError("writer thread wedged")

        scene, api = self._make()
        assert scene._player is not None
        scene._player.stop = wedged  # type: ignore[method-assign]
        scene._apply_vic_hires_bank()
        scene._alloc_scope_buffers()
        with self.assertLogs("c64cast.sid.asid_scene", level="ERROR"):
            scene.teardown()
        self.assertEqual(
            api.regs["0314"], (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        )

    def test_the_midi_port_closes_before_the_machine_is_restored(self):
        """Ordering, not just membership.

        The reader's poll stop is a bounded join that `_pollthread` documents as
        abandoning a worker blocked on the link, and that worker's loop calls
        `_retune_if_due` on every pass — on the buffered path a CIA #1 latch
        write through the player, and pre-arm a handler re-upload over $C000.
        (On the coalesced path `_player` is None and `_retune_if_due` reaches no
        hardware at all, which is also why `kernal IRQ restore` is gated on
        `_player`.) Closing the port is what stops it reading one more `0x31`,
        so it has to run before the restores below and not after them: a retune
        landing afterward hands the next scene the exact CIA state these steps
        exist to undo. The `base teardown` step ahead of them is a no-op for
        this scene (`display_mode` is None), so pinning the port close against
        the restores below pins it against every restore there is.
        """
        order: list[str] = []
        scene, _ = self._make()
        assert scene._player is not None
        # The docstring's "every restore there is" rests on this: give the scene
        # a display mode and `Scene.teardown` becomes a real machine restore
        # running ahead of the port close, which narrows the invariant without
        # touching this test's own steps.
        self.assertIsNone(
            scene.display_mode, "the base teardown step would restore the machine first"
        )
        # Recorded on the port itself rather than on a wrapper method, so the
        # order is asserted against the call teardown actually makes.
        scene._midi_port = SimpleNamespace(close=lambda: order.append("port close"))
        with (
            mock.patch.object(
                scene._player, "stop", side_effect=lambda: order.append("player stop")
            ),
            mock.patch(
                "c64cast.sid.asid_scene.restore_kernal_irq",
                side_effect=lambda *a: order.append("kernal IRQ restore"),
            ),
        ):
            scene._apply_vic_hires_bank()
            scene._alloc_scope_buffers()
            scene.teardown()
        self.assertEqual(order, ["port close", "player stop", "kernal IRQ restore"])

    def test_teardown_hands_the_irq_back_without_trusting_the_player(self):
        """The scene owns the promise that the next scene gets a quiescent C64.
        It must not delegate that to the player's own bookkeeping: the player's
        writer thread can outlive its bounded join, and an orphaned ASID handler
        rewrites the whole REU control block ($DF02-$DF08 + a $91 fetch-exec) at
        up to 960 Hz — which the next scene's audio pump reads back as its live
        write head."""
        from c64cast.hw.c64 import KERNAL, kernal_cia1_latch
        from c64cast.sid import asid_player as ap

        scene, api = self._make()
        assert scene._player is not None
        # A player that restores nothing, i.e. one whose writer outlived it.
        scene._player.stop = lambda: None  # type: ignore[method-assign]
        scene._apply_vic_hires_bank()
        scene._alloc_scope_buffers()
        scene.teardown()
        self.assertEqual(
            api.regs["0314"], (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        )
        latch = kernal_cia1_latch("NTSC")
        self.assertEqual(
            api.memories[f"{ap.CIA1.TIMER_A_LO:04X}"],
            f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}",
        )

    def test_reactivation_forgets_the_previous_streams_cadence(self):
        """Playlists reuse scene instances, and setup() hands `_frame_rate_hz`
        straight to the player, which programs CIA #1 from it before a single
        byte of the new stream arrives. Lap 1's host must not get to pick lap
        2's IRQ rate."""
        from c64cast.sid import asid_player as ap

        scene, api = self._make()
        with _stub_port(scene), quiet_logging():
            # The hostile 0x31 warns; its own test asserts that message.
            scene.setup()
            # frame_delta_us = 1 → 1 MHz, clamped to the band ceiling.
            scene._handle_sysex((asid.ASID_MANUFACTURER_ID, asid.CMD_SPEED, 0x01, 0x01, 0x00, 0x00))
            self.assertAlmostEqual(scene._frame_rate_hz, ap.MAX_FRAME_RATE_HZ, delta=1.0)
            scene.teardown()
            scene.setup()
        try:
            self.assertAlmostEqual(scene._frame_rate_hz, 60.0, delta=0.1)
            expected = ap.cia1_latch_for_rate(60.0, "NTSC")
            self.assertEqual(
                api.memories[f"{ap.CIA1.TIMER_A_LO:04X}"],
                f"{expected & 0xFF:02X}{(expected >> 8) & 0xFF:02X}",
            )
        finally:
            with quiet_logging():
                scene.teardown()

    def test_reactivation_puts_the_ring_player_back_to_one_chip(self):
        # A stale chip count is what lets a later remap SHRINK the ring — the
        # reinit guard only compares against the count the player already holds.
        scene, _ = self._make()
        assert scene._player is not None
        # Set the layout directly, so the precondition holds whatever reset()
        # does — seeding it through the method under test would let a reset()
        # that does nothing at all pass this vacuously.
        scene._player._set_layout(4)  # what a multi-SID lap 1 leaves behind
        with _stub_port(scene):
            scene.setup()
        try:
            self.assertEqual(scene._player.n_chips, 1)
        finally:
            scene.teardown()

    def test_wants_reu_flags_buffered_asid(self):
        from c64cast.app.config import Config, SceneCfg
        from c64cast.hw.hw_provision import wants_reu

        cfg = Config()
        cfg.scenes = [SceneCfg(type="asid", asid_buffered_player="on")]
        wants, reasons = wants_reu(cfg)
        self.assertTrue(wants)
        self.assertTrue(any("asid" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
