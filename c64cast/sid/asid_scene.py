"""ASID → SID scene. Plays an incoming ASID MIDI stream on the real SID(s) and
visualizes the voices as a full-screen hires oscilloscope.

Any ASID *host* — DeepSID (browser), SIDFactory II, Plogue chipsynth C64,
Elektron ASID-XP — streams packed SID register writes over MIDI SysEx; this
scene decodes them (see :mod:`c64cast.sid.asid`), writes them to the real SID chip
over DMA, and drives the shared 3-voice oscilloscope. It turns c64cast into an
ASID *client* whose SID happens to be genuine hardware on the U64/TeensyROM,
with the scope on HDMI.

**Multi-SID (U64 only).** ASID streams can carry several SID chips (commands
``0x50``-``0x5F`` = SID2..SID17). On the Ultimate 64 — which can be *dynamically
configured for up to 8 SIDs* across two physical sockets and two "UltiSID" FPGA
cores — this scene detects the chip count from the stream, configures the U64's
SID address map live over the REST config API (preferring socketed **physical**
SIDs; see :mod:`c64cast.sid.asid_sidmap`), and routes each chip's register writes to
its own address. The scope subdivides each of the three voice rows horizontally,
one window per chip (voice 1 of every chip in row 1, side by side, etc.). The
prior config is snapshotted and restored on teardown. On backends without the
config API (TeensyROM) or with multi-SID disabled, extra chips are downmixed to
the primary SID with a one-time warning.

This is the sibling of :class:`~c64cast.sid.midi_scene.MidiScene`: same
:class:`~c64cast.sid.voice_scope.VoiceScopeRenderer` visualization, same MIDI-port
plumbing, same 25-byte ``$D400-$D418`` register shadow per chip feeding a
host-side :class:`~c64cast.sid.sidemu.SIDEmulator`. The difference is the input:
MidiScene *synthesizes* SID writes from notes/CCs, while AsidScene receives the
finished register bytes and just relays them.

**Two playback paths.** The default *coalesced* path folds register frames into
per-chip shadows and flushes one ``$D400-$D418`` block write per dirty chip at a
bounded rate (see ``_FLUSH_INTERVAL_S``); a within-frame gate-off→gate-on "hard
restart" additionally emits the first control value just before the block so the
pulse reaches the chip. This is host-driven and drops intermediate frames on
multispeed tunes. The *buffered* path (U64 only, ``asid_buffered_player``) hands
frames to a C64-side REU ring player (:mod:`c64cast.sid.asid_player`) that consumes
one frame per CIA #1 Timer A tick at the ASID cadence — cycle-accurate, no frames
dropped (arps/vibrato/hard restarts survive multispeed). The reader still folds
every frame into the shadows + host emulators so the oscilloscope tracks all
voices in both paths; only the SID *write* mechanism differs.

Display is bitmap-only (hires), so PETSCII overlays don't apply. Requires the
``midi`` extra (``uv tool install --force 'c64cast[all]'``) — ASID rides the same MIDI
transport.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from functools import partial

from c64cast._midi import MIDI_AVAILABLE, open_input_port, poll_pending
from c64cast._pollthread import PollThread
from c64cast._teardown import run_teardown_steps
from c64cast.hw.c64 import CIA2, CLOCK_NTSC, CLOCK_PAL, SID, VIC_BANK_0
from c64cast.scenes.scenes import Scene
from c64cast.video.palette import C64_COLORS

from . import asid
from .asid_player import (
    AsidRingPlayer,
    clamp_frame_rate,
    fit_frame_to_budget,
    frame_cycle_cost,
    new_truncation_log,
    pack_slot,
    restore_kernal_irq,
    serialize_frame,
)
from .asid_sidmap import MAX_SIDS, SidMap, plan_sid_map
from .emusid_mixer import apply_emusid_routing
from .sid_hw_config import SidHwSession, apply_sid_map, detect_sockets
from .sid_panning import apply_panning, sources_for_addresses
from .sid_resolved import log_resolved_audio
from .sid_volume import apply_volume
from .sidemu import SID_REG_COUNT, SIDEmulator, primary_waveform
from .voice_scope import (
    D018_HIRES_BITMAP,
    VoiceScopeRenderer,
    _layout_lr,
    restore_char_mode_display,
)

log = logging.getLogger(__name__)

# Max rate at which coalesced register frames are flushed to the SID. 60 Hz
# covers PAL/NTSC single-speed frame rates and keeps bursts / high-multispeed
# tunes from outrunning the ~200 writes/sec DMA ceiling. See _reader().
_FLUSH_INTERVAL_S = 1.0 / 60.0

# How often a wire 0x31 may actually retune the CIA. Requests inside the window
# are coalesced (newest wins) rather than queued, so a sender alternating rates
# costs exactly what a sender repeating one rate costs. See `_retune_if_due`.
_SPEED_RETUNE_INTERVAL_S = 0.25

# Idle voice strips draw in this gray (matches MidiScene / WaveformScene).
_IDLE_GRAY = "gray"
_ENV_SILENCE_EPS = 1e-3

# SID waveform-select bit → short label for the info row.
_WAVE_ABBREV = {
    SID.WAVE_TRIANGLE: "TRI",
    SID.WAVE_SAWTOOTH: "SAW",
    SID.WAVE_PULSE: "PUL",
    SID.WAVE_NOISE: "NOI",
}

# Offset of the master volume / filter-mode register within the shadow.
_MODE_VOL_OFFSET = SID.MODE_VOL - SID.BASE

# The exact (category, item) set the teardown snapshot round-trips lives with
# the snapshot machinery: sid_hw_config.MANAGED_* / SidHwSession.


class AsidScene(VoiceScopeRenderer, Scene):
    """Receive an ASID MIDI stream and play it on the real SID(s) + oscilloscope
    (see the module docstring)."""

    WANTS_AUDIO_LOCK = True

    def __init__(
        self,
        api,
        audio,
        port: str | None = None,
        voice_colors: list[str] | None = None,
        color_mode: str = "per_voice",
        waveform_colors: dict | None = None,
        time_base: str = "wallclock",
        auto_cycles: float = 4.0,
        persistence: str = "off",
        scroll_columns: int | list[int] = 0,
        target_fps: float | None = None,
        system: str = "NTSC",
        multi_sid: bool = True,
        max_sids: int | None = None,
        buffered_player: str = "auto",
        sid_panning: Sequence[int | str] | None = None,
        sid_volume: Sequence[int | str] | None = None,
        name: str = "ASID",
    ):
        super().__init__(api, audio, None, name)
        if not MIDI_AVAILABLE:
            raise RuntimeError(
                "AsidScene requires mido + python-rtmidi (uv tool install --force 'c64cast[all]')"
            )

        self.port_name = port
        self.system = system

        # This scene *is* the stream — one MIDI input port — so the two
        # wire-triggered report budgets belong here rather than at either
        # module's top level, where they were per-process: in ensemble mode a
        # flooding system then suppressed another system's first-ever report of
        # the same condition. See :mod:`c64cast._wire_log`.
        self._recipe_log = asid.new_recipe_log()
        self._truncation_log = new_truncation_log()
        # The system the *machine* runs, kept apart from `self.system` because a
        # wire `0x31` retunes that one to whatever standard the tune declares.
        # Anything restoring a hardware default (the kernal CIA #1 latch is
        # PAL/NTSC-specific) has to use this: writing PAL's $4025 back on an NTSC
        # machine leaves the jiffy clock ~3.8% fast for every scene after.
        # AsidRingPlayer is constructed with the same value for the same reason.
        self._machine_system = system

        # Voice trace colors — pad the configured names to 3 with C64-friendly
        # defaults; the scope mixin requires at least 3.
        default_colors = ["light green", "cyan", "yellow"]
        names = list(voice_colors) if voice_colors else list(default_colors)
        if len(names) < SID.N_VOICES:
            names = (names + default_colors[len(names) :])[: SID.N_VOICES]

        # Bitmap display: fixed VIC bank 0 ($0400 screen / $2000 bitmap). No
        # relocation — AsidScene uploads no payload and leaves the audio ring
        # idle, so bank 0's display regions are always free.
        self._screen_base = VIC_BANK_0.SCREEN
        self._bitmap_base = VIC_BANK_0.BITMAP
        self._dd00 = CIA2.PORT_A_BANK_0
        self._d018 = D018_HIRES_BITMAP

        # Multi-SID: honored only when enabled AND the backend exposes the
        # U64 multi-SID config surface. Off ⇒ extra chips downmix to the primary.
        self._multi_sid = multi_sid and bool(getattr(api.profile, "supports_sid_config", False))
        self._max_sids = MAX_SIDS if max_sids is None else max(1, min(max_sids, MAX_SIDS))

        # Buffered C64-side ring player (cycle-accurate multispeed) — U64 only
        # (needs bus-clean reu_write). "auto" ⇒ on when the backend has an REU;
        # "on" warns + falls back on a no-REU backend; "off" forces the coalesced
        # path. When active, the reader serializes frames into the REU ring
        # instead of coalescing block writes (both paths still feed the scope).
        supports_reu = bool(getattr(api.profile, "supports_reu", False))
        want = (buffered_player or "auto").lower()
        self._use_buffered_player = want in ("auto", "on") and supports_reu
        if want == "on" and not supports_reu:
            log.warning(
                "AsidScene: asid_buffered_player = 'on' but the backend has no REU "
                "— falling back to the coalesced flush path"
            )
        self._player: AsidRingPlayer | None = None

        # One host-side SID model + register shadow per chip, pre-allocated to
        # the max so the reader thread never grows the lists (the render thread
        # reads them). Only the first `_active_chips` are displayed / routed.
        self._emulators = [SIDEmulator(system=system) for _ in range(MAX_SIDS)]
        self.emulator = self._emulators[0]  # scope mixin's primary source
        self._sid_shadows = [bytearray(SID_REG_COUNT) for _ in range(MAX_SIDS)]
        self._reg_lock = threading.Lock()
        self._video_hz = 50.0 if system.upper() == "PAL" else 60.0
        self._poll_dt = 1.0 / self._video_hz
        self._poll: PollThread | None = None

        # Active chip count + their $Dxxx base addresses. Starts single (chip 0
        # at $D400) and grows via _reconfigure_chips when the stream reveals more.
        self._active_chips = 1
        self._chip_addresses: list[int] = [SID.BASE]

        # Half the system video rate (30 NTSC / 25 PAL) like MidiScene — an
        # oscilloscope reads fine at half-rate and it halves per-frame bitmap
        # DMA. An explicit target_fps still wins.
        if target_fps is None:
            target_fps = self._video_hz / 2.0
        self.target_fps = float(target_fps)

        self._init_scope_knobs(
            color_mode=color_mode,
            voice_colors=names,
            waveform_colors=waveform_colors,
            time_base=time_base,
            auto_cycles=auto_cycles,
            persistence=persistence,
            scroll_columns=scroll_columns,
            frame_time_s=1.0 / self.target_fps,
            n_windows=1,
        )

        # Per-(voice, chip) display state, change-detected in process_frame.
        self._window_sounding = [[False] * MAX_SIDS for _ in range(SID.N_VOICES)]
        self._last_window_wave = [[-1] * MAX_SIDS for _ in range(SID.N_VOICES)]

        # ASID stream state.
        self._playing: bool = False
        self._status_text: str = ""  # latest 0x4F display text
        self._chip_type: str | None = None
        # Pending register flush: the reader thread accumulates into per-chip
        # shadows + control-first maps and flushes at _FLUSH_INTERVAL_S.
        self._pending_flush = False  # any chip dirty (kept for the fast poll check)
        self._dirty_chips: set[int] = set()
        self._pending_ctrl_first: dict[int, dict[int, int]] = {}  # chip -> {voice: fval}
        self._warned_cmds: set[int] = set()
        self._warned_downmix = False
        # Buffered-path per-frame accumulation (reader thread only). A frame is
        # every 0x50-0x5F between two chip-0 (0x4E) messages; emitted on the next
        # 0x4E / start / stop. Deltas (not the full shadow) are serialized, so
        # arps/vibrato/hard restarts replay exactly. The 0x30 recipe (if any)
        # orders the writes + carries their inter-write waits.
        self._frame_regs: dict[int, dict[int, int]] = {}  # chip -> {offset: value}
        self._frame_ctrl_first: dict[int, dict[int, int]] = {}  # chip -> {voice: fval}
        self._frame_has_data = False
        self._recipe: list[tuple[int, int]] | None = None
        self._frame_rate_hz = self._video_hz  # ASID cadence (0x31 retunes it)
        # 0x31 retune throttle: the newest *unclamped* rate the wire asked for,
        # the one last applied to hardware, and when that happened. Injectable
        # clock so the window can be tested without sleeping on it.
        self._speed_request_hz = self._video_hz
        self._applied_speed_hz = self._video_hz
        self._last_retune_at = float("-inf")
        self._monotonic = time.monotonic
        # One-shot: a frame whose ops outrun the consume period says so once.
        self._warned_frame_budget = False
        # Highest chip index seen on the wire (reader thread); process_frame
        # compares against _active_chips to trigger a live remap on the main
        # thread (avoids mutating display state from the reader).
        self._max_chip_seen = 0
        # Snapshot of the SID-address config taken before the first remap, for
        # restore on teardown. Empty until a remap happens.
        self._sid_session = SidHwSession(api)
        self._socket_present = (False, False)
        self._remap_failed = False  # one-shot WARNING gate for a failing remap
        # [ultimate64].sid_panning — empty means the auto spread. Applied at
        # setup for the initial single chip and re-applied on every remap.
        self._sid_panning = list(sid_panning or ())
        # [ultimate64].sid_volume — empty means "0 dB for a source that would
        # otherwise be inaudible". Applied alongside panning: an ASID stream
        # that grows past 2 chips routes the rest onto UltiSID cores, which are
        # silent on any machine whose mixer leaves them at OFF.
        self._sid_volume = list(sid_volume or ())

        self._midi_port = None
        self._reader_poll = PollThread(
            self._reader, name="asid-reader", manual=True, join_timeout=1.0
        )
        self._dirty = True  # force first text-row paint

        # Construct the ring player up front (its queue accepts pushes before the
        # writer thread is armed) so the reader can enqueue frames as soon as it
        # starts; setup() arms it once the bitmap is up.
        if self._use_buffered_player:
            self._player = AsidRingPlayer(api, system=system, n_chips=1)

    # ---- MIDI plumbing -------------------------------------------------------
    def _open_port(self):
        self._midi_port, _ = open_input_port(self.port_name, label="AsidScene")

    def _reader(self, stop: threading.Event):
        port = self._midi_port
        if port is None:
            return
        # Drain pending SysEx each pass into the per-chip register shadows, then
        # flush coalesced block writes at a bounded rate. An ASID host sends up
        # to ~50-60 frames/sec (higher for multispeed tunes), each touching most
        # of the 25 registers of one or more chips; applying every frame as its
        # own DMA burst would outrun the U64's write ceiling and drop the
        # socket. Coalescing to the latest shadows keeps the link healthy (a
        # multispeed tune loses some intermediate frames — a known v1
        # limitation).
        #
        # `poll_pending` (not mido's `iter_pending`) bounds the drain and
        # re-checks `stop`: a flood arriving faster than it is retired must not
        # be able to starve the flush below — the SID would hold whatever was
        # last written and keep sounding — nor outlive `teardown`'s bounded
        # join, which abandons a still-running reader that could then land a
        # block write after `silence_sid()`.
        last_flush = 0.0
        try:
            while not stop.is_set():
                for msg in poll_pending(port, stop):
                    if msg.type == "sysex":
                        self._handle_sysex(msg.data)
                now = time.time()
                if self._pending_flush and now - last_flush >= _FLUSH_INTERVAL_S:
                    self._flush_to_sid()
                    last_flush = now
                # A 0x31 that landed inside the retune window is latched, not
                # dropped, so the loop is what applies it once the window opens.
                self._retune_if_due()
                time.sleep(0.001)  # 1 ms poll
        except Exception:
            log.exception("AsidScene reader crashed")

    def _chip_for(self, chip_index: int) -> int:
        """Map an ASID chip index to the shadow/emulator slot we use.

        Multi-SID enabled: the index itself while it is within ``_max_sids``;
        anything beyond that downmixes to the primary SID (slot 0), warned once
        — the cap drops the surplus chip rather than renumbering it, which is
        what ``asid_max_sids``'s own documentation promises. Disabled:
        everything downmixes to slot 0 with the same one-time warning."""
        if not self._multi_sid:
            if chip_index > 0 and not self._warned_downmix:
                self._warned_downmix = True
                log.warning(
                    "AsidScene: multi-SID stream on a backend without the config "
                    "API (or --no multi-sid) — downmixing extra chips to the primary SID"
                )
            return 0
        if chip_index >= self._max_sids:
            if not self._warned_downmix:
                self._warned_downmix = True
                log.warning(
                    "AsidScene: stream uses SID chip %d beyond the %d-SID limit — "
                    "downmixing the surplus to the primary SID",
                    chip_index + 1,
                    self._max_sids,
                )
            return 0
        return chip_index

    def _handle_sysex(self, data) -> None:
        """Decode one ASID SysEx message and fold it into the pending state.

        Runs on the reader thread. The per-chip register shadows + frame
        accumulators are only ever touched here (single-threaded), so they need
        no lock; the emulators (shared with the render + envelope threads) are
        guarded in _flush_to_sid / _emit_buffered_frame."""
        update = asid.decode(data, recipe_log=self._recipe_log)
        if update is None:
            return  # foreign SysEx — not ASID
        if update.dropped:
            if update.command not in self._warned_cmds:
                self._warned_cmds.add(update.command)
                log.warning(
                    "AsidScene: ignoring unsupported ASID command 0x%02X (OPL-FM)",
                    update.command,
                )
            return
        if update.command == asid.CMD_TIMING:
            # 0x30 write-order/wait recipe. Only the buffered player replays it
            # (the coalesced path writes the whole image at once); store it for
            # subsequent frames either way.
            self._recipe = update.timing_recipe or None
            return
        if update.regs:
            chip = self._chip_for(update.chip_index)
            # Buffered path: a chip-0 (0x4E) message starts a new frame, so emit
            # the accumulated previous frame first.
            if self._use_buffered_player and update.chip_index == 0 and self._frame_has_data:
                self._emit_buffered_frame()
            shadow = self._sid_shadows[chip]
            for offset, value in update.regs.items():
                shadow[offset] = value & 0xFF
            if self._use_buffered_player:
                fr = self._frame_regs.setdefault(chip, {})
                fr.update((o, v & 0xFF) for o, v in update.regs.items())
                if update.control_first:
                    cf = self._frame_ctrl_first.setdefault(chip, {})
                    cf.update(update.control_first)
                self._frame_has_data = True
            else:
                if update.control_first:
                    cf = self._pending_ctrl_first.setdefault(chip, {})
                    for voice, fval in update.control_first.items():
                        cf[voice] = fval
                self._dirty_chips.add(chip)
                self._pending_flush = True
            if self._multi_sid and chip > self._max_chip_seen:
                # Grow handled on the main thread (process_frame) to keep display
                # mutation off the reader thread. The request comes from the
                # *mapped* slot, not the wire index: an index past `_max_sids`
                # was already downmixed to slot 0 above, so growing on it would
                # map, pan and re-init the ring player for chips no data can
                # ever reach (every window past the first permanently blank).
                self._max_chip_seen = chip
        if update.text is not None:
            self._status_text = update.text
            self._dirty = True
        if update.playing is not None:
            # Emit any partial frame before a start/stop boundary (buffered path).
            if self._use_buffered_player and self._frame_has_data:
                self._emit_buffered_frame()
            if update.playing != self._playing:
                self._playing = update.playing
                self._dirty = True
        if update.system is not None and update.system != self.system:
            self.system = update.system
            with self._reg_lock:
                for emu in self._emulators:
                    emu.clock = CLOCK_NTSC if update.system == "NTSC" else CLOCK_PAL
            self._video_hz = 50.0 if update.system.upper() == "PAL" else 60.0
            self._dirty = True
        if update.command == asid.CMD_SPEED:
            self._apply_speed(update)
        if (
            update.chip_index == 0
            and update.chip_type is not None
            and update.chip_type != self._chip_type
        ):
            self._chip_type = update.chip_type
            self._dirty = True

    def _apply_speed(self, update: asid.AsidUpdate) -> None:
        """Latch the consume rate a 0x31 message asks for, and retune if due.

        Frame delta (µs) wins when present; else the speed multiplier scales the
        system video rate. A no-op for the coalesced path (it flushes on its own
        timer).

        Deriving the rate is all this does per message, and that is the point —
        see :meth:`_retune_if_due` for why the hardware half is rate-limited."""
        if update.frame_delta_us:
            rate = 1_000_000.0 / update.frame_delta_us
        elif update.speed_multiplier:
            rate = self._video_hz * update.speed_multiplier
        else:
            rate = self._video_hz
        self._speed_request_hz = rate
        self._retune_if_due()

    def _retune_if_due(self) -> None:
        """Apply the latched 0x31 rate, at most once per `_SPEED_RETUNE_INTERVAL_S`.

        Called for every 0x31 and again from the reader loop, so a request that
        lands inside the window is *coalesced* — the newest one wins and goes out
        when the window expires — rather than queued or dropped. That distinction
        is the whole defense: a retune is a blocking CIA-latch write plus a
        `flush()` round trip on the single-connection DMA socket the render path
        and `AudioStreamer` share (and, pre-arm, a full handler re-upload), and
        `_handle_sysex` runs on the MIDI reader thread. Applying one per message
        let a sender at an ordinary 60 Hz frame rate spend the whole ~200/s link
        budget on CIA latches *and* back rtmidi's unbounded input queue up behind
        the drain, which is what starves the register flush and outlives
        teardown's bounded join. Deduplicating the *argument* — which is all this
        used to do — was defeated by alternating any two rates, and 999/1000 Hz
        are both in band, so not even a clamp warning fired.

        Forwarding stays unconditional on *arming*: a 0x31 almost always arrives
        before the prebuffer fills, and dropping it decimates the tune to the
        video rate. The clamp runs here rather than on the derive path so a flood
        of out-of-band requests cannot spend the log either."""
        rate = self._speed_request_hz
        if rate == self._applied_speed_hz:
            return
        now = self._monotonic()
        if now - self._last_retune_at < _SPEED_RETUNE_INTERVAL_S:
            return
        self._last_retune_at = now
        self._applied_speed_hz = rate
        self._frame_rate_hz = clamp_frame_rate(rate)
        if self._use_buffered_player and self._player is not None:
            self._player.set_frame_rate(self._frame_rate_hz)

    def _emit_buffered_frame(self) -> None:
        """Serialize the accumulated frame (all mapped chips) into one ring slot,
        push it to the player, and mirror it into the host emulators for the
        scope. Reader thread.

        Only the chips actually serialized have their accumulators cleared. A
        chip's *first* frame arrives before `process_frame` has run
        `_reconfigure_chips`, so it has no mapped address yet — and because this
        path serializes *deltas*, clearing it would lose that chip's initial
        ADSR / pulse-width / control setup for good (the host never re-sends
        it). Carrying it forward is what the coalesced `_flush_to_sid` does for
        the same case."""
        player = self._player
        if player is not None and self._frame_has_data:
            all_ops: list[tuple[int, int, int]] = []
            emu_updates: list[tuple[int, tuple[bool, ...] | None]] = []
            for chip in range(self._active_chips):
                regs = self._frame_regs.get(chip)
                if not regs or chip >= len(self._chip_addresses):
                    continue
                base = self._chip_addresses[chip]
                ctrl_first = self._frame_ctrl_first.get(chip, {})
                all_ops.extend(serialize_frame(regs, ctrl_first, base, self._recipe))
                retrigger = None
                if ctrl_first:
                    mask = [False] * SID.N_VOICES
                    for voice in ctrl_first:
                        mask[voice] = True
                    retrigger = tuple(mask)
                emu_updates.append((chip, retrigger))
            all_ops = self._fit_to_frame_budget(all_ops, player)
            player.push_frame(
                pack_slot(all_ops, player.slot_size, truncation_log=self._truncation_log)
            )
            # Mirror into the emulators (scope) — the C64 plays the real SID.
            # Only the serialized chips, so the scope can't show a voice
            # configured on hardware that has not been programmed yet.
            with self._reg_lock:
                for chip, retrigger in emu_updates:
                    self._emulators[chip].update_registers(
                        bytes(self._sid_shadows[chip]), retrigger=retrigger
                    )
            for chip, _ in emu_updates:
                self._frame_regs.pop(chip, None)
                self._frame_ctrl_first.pop(chip, None)
        else:
            # No player to carry anything forward to — drop the accumulators
            # rather than growing them without bound.
            self._frame_regs.clear()
            self._frame_ctrl_first.clear()
        self._frame_has_data = bool(self._frame_regs)

    def _fit_to_frame_budget(
        self, ops: list[tuple[int, int, int]], player: AsidRingPlayer
    ) -> list[tuple[int, int, int]]:
        """Hold one slot's ops to what the 6510 can execute between two consume
        ticks, warning once per stream when it has to.

        The op *count* is bounded at the decoder and by `MAX_OPS_PER_CHIP`, but
        their *cost* is not: a `0x30` recipe supplies a wait per write straight
        off the wire, and 28 maximum waits are already over half a 60 Hz NTSC
        frame for a single chip. An overrunning frame is not a dropped frame —
        the CIA fires again before the handler returns, so the 6510 stays inside
        it and the kernal tail (jiffy clock, SCNKEY) stops for as long as the
        stream keeps it up."""
        budget = player.frame_cycle_budget()
        cost = frame_cycle_cost(ops)
        if cost <= budget:
            return ops
        fitted = fit_frame_to_budget(ops, budget)
        if not self._warned_frame_budget:
            self._warned_frame_budget = True
            log.warning(
                "AsidScene: a frame's %d ops cost %d C64 cycles but one consume tick "
                "allows %d — scaling the 0x30 recipe's inter-write waits down to fit "
                "(a frame longer than the tick period keeps the 6510 in the ASID IRQ)",
                len(ops),
                cost,
                budget,
            )
        return fitted

    def _flush_to_sid(self) -> None:
        """Write the accumulated per-chip register shadows to the real SID(s)
        (reader thread). Flushes each dirty chip to its mapped address.

        Hard-restart first-writes go out as individual writes *before* that
        chip's coalesced block (which lands the second/final control value), so
        the gate-off pulse reaches the chip. The same case is invisible to the
        emulator's gate-edge detection (the shadow's prior control byte was
        gated), so flag it via `retrigger`."""
        flushed: set[int] = set()
        for chip in sorted(self._dirty_chips):
            if chip >= len(self._chip_addresses):
                continue  # not remapped yet — stays dirty until _reconfigure_chips
            flushed.add(chip)
            base = self._chip_addresses[chip]
            shadow = self._sid_shadows[chip]
            ctrl_first = self._pending_ctrl_first.pop(chip, None)
            retrigger: tuple[bool, ...] | None = None
            if ctrl_first:
                for voice, fval in ctrl_first.items():
                    addr = base + voice * SID.BYTES_PER_VOICE + SID.OFF_CONTROL
                    self.api.write_memory(f"{addr:04X}", f"{fval & 0xFF:02X}")
                mask = [False] * SID.N_VOICES
                for voice in ctrl_first:
                    mask[voice] = True
                retrigger = tuple(mask)
            # One coalesced block write of the whole 25-byte image to this chip.
            self.api.write_regs(f"{base:04X}", *shadow)
            with self._reg_lock:
                self._emulators[chip].update_registers(bytes(shadow), retrigger=retrigger)
        self._dirty_chips -= flushed
        # Stay pending if chips are deferred (awaiting remap) or ctrl-firsts remain.
        self._pending_flush = bool(self._dirty_chips) or bool(self._pending_ctrl_first)

    # ---- multi-SID configuration ---------------------------------------------
    def _reconfigure_chips(self, n: int) -> None:
        """Grow the active SID map to `n` chips: configure the U64 address map
        live, update routing, and reflow the split scope. Runs on the main
        (render) thread from process_frame.

        The hardware half is guarded, because it is the half that can raise:
        `AsidRingPlayer.reinit` restarts the writer over the shared DMA link. A
        raise escaping here reaches `Playlist._render_scene_frame`, which retires
        a crashing scene for good — so one transient link hiccup used to end the
        whole ASID set. Leaving the display state untouched keeps the growth
        guard in `process_frame` true, so the next frame retries."""
        n = max(1, min(n, self._max_sids))
        self._sid_session.snapshot()
        sid_map = plan_sid_map(
            n, socket1_present=self._socket_present[0], socket2_present=self._socket_present[1]
        )
        try:
            apply_sid_map(self.api, sid_map)
            self._chip_addresses = list(sid_map.addresses)
            # Resize the buffered ring player for the new chip count (bigger
            # slot). Frames the reader serializes during the brief re-init window
            # are self-describing (n_ops-bounded) and any wrong-sized ones the
            # writer drops, so a couple of transient frames may be skipped — no
            # corruption.
            if self._use_buffered_player and self._player is not None:
                self._player.reinit(sid_map.n)
        except Exception:
            # WARNING once per failure run, DEBUG while it keeps failing: the
            # retry is per rendered frame, so an unrecoverable link would
            # otherwise log 30 times a second.
            if self._remap_failed:
                log.debug("AsidScene: SID remap retry failed", exc_info=True)
            else:
                self._remap_failed = True
                log.warning(
                    "AsidScene: mapping %d SID chip(s) failed — retrying on the next frame",
                    sid_map.n,
                    exc_info=True,
                )
            return
        self._remap_failed = False
        log.info(
            "AsidScene: mapped %d SID chip(s) → %s",
            sid_map.n,
            ", ".join(f"${a:04X}" for a in sid_map.addresses),
        )
        # Reflow the split scope: new window count, then a full bitmap bring-up
        # to clear the old windows' pixels and repaint idle strips + info rows.
        # Panning runs after _set_window_count, which resets the column order.
        #
        # `_active_chips` is published only once the scope has the windows to
        # match, and never before a step that can raise: `reinit` writes to the
        # DMA link, and a link hiccup there used to leave _active_chips ahead of
        # _n_windows — process_frame then indexed past `_scope_emulators()` and
        # raised IndexError on every later frame, which the playlist treats as a
        # crashed scene. Publishing last keeps the growth guard true so the next
        # frame simply retries the remap.
        self._set_window_count(sid_map.n)
        self._active_chips = sid_map.n
        self._apply_sid_mixer(sid_map)
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        self._window_sounding = [[False] * MAX_SIDS for _ in range(SID.N_VOICES)]
        self._last_window_wave = [[-1] * MAX_SIDS for _ in range(SID.N_VOICES)]
        for v in range(SID.N_VOICES):
            self._paint_strip_color_row(v, [C64_COLORS[_IDLE_GRAY]] * self._n_windows)
        self._alloc_scope_buffers()
        self._dirty = True

    def _sid_sources(self, sid_map: SidMap | None) -> Sequence[str | None]:
        """The mixer source playing each active chip, in chip order — straight
        from the map a remap just applied, else from the live config."""
        if sid_map is not None and sid_map.sources:
            return sid_map.sources
        n = sid_map.n if sid_map is not None else self._active_chips
        return sources_for_addresses(self.api, self._chip_addresses[:n])

    def _apply_sid_mixer(self, sid_map: SidMap | None = None) -> None:
        """Pan the active SID chips across the mixer's stereo field and make
        every source they landed on audible ([ultimate64].sid_panning /
        sid_volume). Called at setup for the initial single chip and again after
        every remap, so both track the current chip count — a stream growing
        past 2 chips spills onto UltiSID cores, which are inaudible on a machine
        whose mixer leaves them at OFF. On the emulated-stereo-SID surface
        (U2+) the routing step first points a spare enabled side at any
        uncovered chip address. Originals fold into the same snapshot teardown
        restores, and the scope's columns are reordered to run left-to-right
        across the stereo field."""
        n = sid_map.n if sid_map is not None else self._active_chips
        addresses = self._chip_addresses[:n]
        self._sid_session.fold(apply_emusid_routing(self.api, addresses))
        sources = self._sid_sources(sid_map)
        panning = apply_panning(self.api, sources, self._sid_panning)
        self.set_window_chip_order(panning.window_order)
        self._sid_session.fold(panning.originals)
        self._sid_session.fold(apply_volume(self.api, sources, self._sid_volume))
        # No model requirement to check — an ASID stream carries no PSID header,
        # so the line reports routing and audibility only.
        log_resolved_audio(self.api, addresses)

    def _restore_config(self) -> None:
        self._sid_session.restore()

    # ---- info text rows ------------------------------------------------------
    def _build_title_line(self) -> str:
        """Scene name + SID count (left), play state + chip type (right)."""
        left = self.name[:16]
        if self._active_chips > 1:
            left = f"{left} {self._active_chips}SID"
        state = "PLAYING" if self._playing else "READY"
        if self._chip_type:
            state = f"{self._chip_type} {state}"
        return _layout_lr(left[:24], state)

    def _build_meta_line(self) -> str:
        """The latest 0x4F display text if the host sent any, else a per-voice
        waveform + master-volume summary derived from the primary chip's shadow."""
        if self._status_text:
            return self._status_text[:40].ljust(40)
        with self._reg_lock:
            waves = [primary_waveform(v.control) for v in self.emulator.voices]
        tags = " ".join(f"{i + 1}:{_WAVE_ABBREV.get(w, '---')}" for i, w in enumerate(waves))
        vol = self._sid_shadows[0][_MODE_VOL_OFFSET] & 0x0F
        return _layout_lr(tags, f"VOL {vol:2d}")

    # ---- Scene lifecycle -----------------------------------------------------
    def _reset_stream_state(self) -> None:
        """Forget the previous stream. Playlists reuse scene instances, so every
        field the *wire* owns has to come back to its configured default here or
        lap 2 begins mid-conversation with lap 1's host.

        The cadence fields are the ones that reach hardware: `setup` passes
        `_frame_rate_hz` straight to `AsidRingPlayer.start`, so a stream that
        pushed the rate to the 1000 Hz ceiling and went away had the *next* lap
        program the CIA to it before a single byte arrived. The player's chip
        count is reset for a related reason — its `reinit` guard compares against
        the count it already holds, so a stale 8 is what lets a later remap
        *shrink* the ring — and `reset` drops the previous tune's queued frames
        with it, which would otherwise become the new stream's prebuffer."""
        self.system = self._machine_system
        self._video_hz = 50.0 if self._machine_system.upper() == "PAL" else 60.0
        # The emulator clocks move with `self.system` (the reader only switches
        # them on a *change*), so they have to come back together with it or a
        # lap-2 stream declaring the machine's own standard reads as "no change"
        # and leaves every emulator on lap 1's clock.
        with self._reg_lock:
            for emu in self._emulators:
                emu.clock = CLOCK_PAL if self._video_hz == 50.0 else CLOCK_NTSC
        self._frame_rate_hz = self._video_hz
        self._speed_request_hz = self._video_hz
        self._applied_speed_hz = self._video_hz
        self._last_retune_at = float("-inf")
        self._warned_frame_budget = False
        self._recipe = None
        self._frame_regs.clear()
        self._frame_ctrl_first.clear()
        self._frame_has_data = False
        self._dirty_chips.clear()
        self._pending_ctrl_first.clear()
        self._pending_flush = False
        self._playing = False
        self._status_text = ""
        self._chip_type = None
        # The shadows are the other half of the same carried-forward state, and
        # the one the *coalesced* path puts on hardware: a flush writes the whole
        # 25-byte image, so a lap-2 frame touching four registers would otherwise
        # send lap 1's ADSR, pulse widths and filter settings along with them.
        # Teardown silenced the chips, so zero is also what the hardware holds.
        with self._reg_lock:
            for shadow in self._sid_shadows:
                shadow[:] = bytes(SID_REG_COUNT)
            for emu in self._emulators:
                emu.update_registers(bytes(SID_REG_COUNT))
        if self._player is not None:
            self._player.reset(1)

    def setup(self) -> None:
        super().setup()
        # Playlists reuse scene instances (every lap re-runs setup/teardown), so
        # the multi-SID shape has to come back to single-chip here: teardown put
        # the user's SID addressing back, and a stale `_max_chip_seen` would
        # leave process_frame's growth guard false, so the map would never be
        # re-applied while the scene kept writing chip 1..N to addresses the
        # restored config no longer routes.
        self._active_chips = 1
        self._chip_addresses = [SID.BASE]
        self._max_chip_seen = 0
        self._set_window_count(1)
        self._reset_stream_state()
        # Bitmap bring-up: invalidate the delta cache (previous scene may have
        # used $0400/$2000 for char content), engage hires, paint idle strips +
        # info rows, allocate render buffers, then start the MIDI reader +
        # envelope ticker. No SID pre-programming — the ASID stream sets it all.
        if self._multi_sid:
            self._socket_present = detect_sockets(self.api)
            # Take the SID-address baseline BEFORE the mixer pass folds its
            # originals in. `SidHwSession.snapshot()` is first-call-wins, so a
            # fold makes it a no-op — and `_reconfigure_chips`'s snapshot then
            # captured nothing, leaving the six MANAGED_ADDRESSING_ITEMS that a
            # remote 0x50-0x5F frame rewrites with no restore at teardown.
            self._sid_session.snapshot()
        self._apply_sid_mixer()
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        self._window_sounding = [[False] * MAX_SIDS for _ in range(SID.N_VOICES)]
        self._last_window_wave = [[-1] * MAX_SIDS for _ in range(SID.N_VOICES)]
        for idx in range(SID.N_VOICES):
            self._repaint_voice_color(idx, C64_COLORS[_IDLE_GRAY])
        self._paint_info_rows()
        self._alloc_scope_buffers()
        self._open_port()
        self._reader_poll.start()
        # Arm the buffered ring player after the reader (so its prebuffer can draw
        # from frames already streaming in) but before the scope needs it.
        if self._use_buffered_player and self._player is not None:
            self._player.start(self._frame_rate_hz)
        self._poll = PollThread(self._tick_envelopes, period=self._poll_dt, name="asid-env")
        self._poll.start()
        self._dirty = True

    def _tick_envelopes(self) -> None:
        with self._reg_lock:
            for chip in range(self._active_chips):
                self._emulators[chip].advance_envelopes(self._poll_dt)

    def process_frame(self, current_time: float) -> bool:
        # Grow the SID map on the main thread when the stream revealed more chips.
        if self._max_chip_seen + 1 > self._active_chips:
            self._reconfigure_chips(self._max_chip_seen + 1)
        # Activity coloring per (voice, chip): a sounding window (gated or still
        # decaying) draws in its color; an idle one fades to gray. Change-
        # detected per strip so the screen color write only fires on a change.
        window_emus = self._scope_emulators()
        with self._reg_lock:
            states = [
                [
                    (
                        v.gated() or v.envelope_level > _ENV_SILENCE_EPS,
                        primary_waveform(v.control),
                    )
                    for v in window_emus[c].voices
                ]
                for c in range(self._active_chips)
            ]
        for v_idx in range(SID.N_VOICES):
            strip_changed = False
            window_colors: list[int] = []
            for c in range(self._active_chips):
                sounding, wave = states[c][v_idx]
                if sounding != self._window_sounding[v_idx][c] or (
                    sounding and wave != self._last_window_wave[v_idx][c]
                ):
                    strip_changed = True
                    self._window_sounding[v_idx][c] = sounding
                    self._last_window_wave[v_idx][c] = wave
                color = (
                    self._voice_color_now(v_idx, window_emus[c])
                    if sounding
                    else C64_COLORS[_IDLE_GRAY]
                )
                window_colors.append(color)
            if strip_changed:
                self._paint_strip_color_row(v_idx, window_colors)
        if self._dirty:
            self._paint_info_rows()
            self._dirty = False
        self._render_hires()
        return True

    def teardown(self) -> None:
        # Shut the wire off FIRST, before anything restores the machine. The
        # `base teardown` step ahead of it restores nothing here: this scene and
        # the other two SID scenes pass `display_mode=None`, so `Scene.teardown`
        # takes its `is not None` guard and does nothing. Worth saying, because
        # that base call *is* a machine restore for the video scenes — it
        # unhooks the staged-REU raster IRQ — and reading it that way here makes
        # this invariant look narrower than it is.
        #
        # The reader's poll stop is a bounded join that `_pollthread` documents
        # as abandoning its worker, so a reader blocked in a DMA write survives
        # it — and its loop calls `_retune_if_due` on every pass, which on the
        # buffered path reprograms CIA #1 through the player and (pre-arm)
        # re-uploads the handler over $C000. Closing the port is what stops an
        # abandoned reader reading a further 0x31, so it has to happen before
        # the restores below rather than after them: a retune that lands
        # afterward puts the jiffy IRQ back on the wire's rate and hands the
        # next scene the exact CIA state these steps exist to undo.
        # It narrows that race rather than closing it: a retune already past its
        # own gate still lands.
        #
        # Then the ring player, which restores $0314 → the kernal IRQ tail and
        # the CIA #1 latch, so the C64 stops popping the ring before we silence
        # the SID(s) and restore the display below.
        #
        # Then restore them AGAIN here, unconditionally — meaning not
        # conditional on the player's own `stop()` having succeeded; the two
        # steps above are gated on there *being* a player. The scene owns the
        # promise that the next scene gets a quiescent C64, and it must not
        # delegate that to the player's own bookkeeping: the player's writer
        # thread can outlive its bounded join, and an orphaned ASID handler is
        # not merely noisy — every tick it rewrites the whole REU control block
        # ($DF02-$DF08 + a $91 fetch-exec) at up to 960 Hz, and the next scene's
        # audio pump reads $DF03 back as its live write head. Two idempotent DMA
        # ops buy the guarantee outright — which is why the restore is a step of
        # its own, and not a statement sequenced behind a stop() that can raise.
        port, self._midi_port = self._midi_port, None
        poll, self._poll = self._poll, None
        steps: list[tuple[str, Callable[[], object]]] = [
            ("base teardown", super().teardown),
            ("reader poll stop", self._reader_poll.stop),
        ]
        if port is not None:
            steps.append(("MIDI port close", port.close))
        if poll is not None:
            steps.append(("input poll stop", poll.stop))
        if self._player is not None:
            steps += [
                ("ring player stop", self._player.stop),
                (
                    "kernal IRQ restore",
                    partial(restore_kernal_irq, self.api, self._machine_system),
                ),
            ]
        steps += [
            (f"silence SID at ${base:04X}", partial(self._silence_chip, base))
            for base in self._chip_addresses
            if base != SID.BASE
        ]
        steps += [
            ("primary SID silence", self.api.silence_sid),
            ("SID address config restore", self._restore_config),
            ("char-mode display restore", partial(restore_char_mode_display, self.api)),
            ("flush", self.api.flush),
        ]
        run_teardown_steps(log, type(self).__name__, steps)

    def _silence_chip(self, base: int) -> None:
        self.api.write_regs(f"{base:04X}", *bytes(SID_REG_COUNT))
