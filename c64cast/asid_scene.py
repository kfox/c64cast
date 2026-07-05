"""ASID → SID scene. Plays an incoming ASID MIDI stream on the real SID(s) and
visualizes the voices as a full-screen hires oscilloscope.

Any ASID *host* — DeepSID (browser), SIDFactory II, Plogue chipsynth C64,
Elektron ASID-XP — streams packed SID register writes over MIDI SysEx; this
scene decodes them (see :mod:`c64cast.asid`), writes them to the real SID chip
over DMA, and drives the shared 3-voice oscilloscope. It turns c64cast into an
ASID *client* whose SID happens to be genuine hardware on the U64/TeensyROM,
with the scope on HDMI.

**Multi-SID (U64 only).** ASID streams can carry several SID chips (commands
``0x50``-``0x5F`` = SID2..SID17). On the Ultimate 64 — which can be *dynamically
configured for up to 8 SIDs* across two physical sockets and two "UltiSID" FPGA
cores — this scene detects the chip count from the stream, configures the U64's
SID address map live over the REST config API (preferring socketed **physical**
SIDs; see :mod:`c64cast.asid_sidmap`), and routes each chip's register writes to
its own address. The scope subdivides each of the three voice rows horizontally,
one window per chip (voice 1 of every chip in row 1, side by side, etc.). The
prior config is snapshotted and restored on teardown. On backends without the
config API (TeensyROM) or with multi-SID disabled, extra chips are downmixed to
the primary SID with a one-time warning.

This is the sibling of :class:`~c64cast.midi_scene.MidiScene`: same
:class:`~c64cast.voice_scope.VoiceScopeRenderer` visualization, same MIDI-port
plumbing, same 25-byte ``$D400-$D418`` register shadow per chip feeding a
host-side :class:`~c64cast.sidemu.SIDEmulator`. The difference is the input:
MidiScene *synthesizes* SID writes from notes/CCs, while AsidScene receives the
finished register bytes and just relays them.

Register frames are coalesced and flushed to the SID at a bounded rate (see
``_FLUSH_INTERVAL_S``). Each flush is one ``$D400-$D418`` block write per dirty
chip; a within-frame gate-off→gate-on "hard restart" additionally emits the
first control value just before the block so the pulse reaches the chip.

Display is bitmap-only (hires), so PETSCII overlays don't apply. Requires the
``midi`` extra (``pip install c64cast[midi]``) — ASID rides the same MIDI
transport.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import asid
from ._pollthread import PollThread
from .asid_sidmap import (
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_TYPE,
    MAX_SIDS,
    plan_sid_map,
)
from .c64 import CIA2, CLOCK_NTSC, CLOCK_PAL, SID, VIC_BANK_0, RegionID
from .palette import C64_COLORS
from .scenes import Scene
from .sidemu import SID_REG_COUNT, SIDEmulator, primary_waveform
from .voice_scope import (
    D018_HIRES_BITMAP,
    META_ROW,
    METADATA_TEXT_COLOR,
    TITLE_ROW,
    TITLE_TEXT_COLOR,
    VoiceScopeRenderer,
    _layout_lr,
)

log = logging.getLogger(__name__)

# Typed as Any so Pyright doesn't flag mido.* as attributes of None — the
# MIDI_AVAILABLE flag is the runtime guard. Mirrors midi_scene.py.
try:
    import mido as _mido

    mido: Any = _mido
    MIDI_AVAILABLE = True
except ImportError:
    mido = None
    MIDI_AVAILABLE = False


# Max rate at which coalesced register frames are flushed to the SID. 60 Hz
# covers PAL/NTSC single-speed frame rates and keeps bursts / high-multispeed
# tunes from outrunning the ~200 writes/sec DMA ceiling. See _reader().
_FLUSH_INTERVAL_S = 1.0 / 60.0

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

# Config items snapshotted before a multi-SID remap and restored on teardown.
_MANAGED_ADDRESSING_ITEMS = (
    "SID Socket 1 Address",
    "SID Socket 2 Address",
    "UltiSID 1 Address",
    "UltiSID 2 Address",
    "UltiSID Range Split",
    "Auto Address Mirroring",
)
_MANAGED_SOCKET_ITEMS = ("SID Socket 1", "SID Socket 2")


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
        name: str = "ASID",
    ):
        super().__init__(api, audio, None, name)
        if not MIDI_AVAILABLE:
            raise RuntimeError(
                "AsidScene requires mido + python-rtmidi (pip install c64cast[midi])"
            )

        self.port_name = port
        self.system = system

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
        # config API (Ultimate REST). Off ⇒ extra chips downmix to the primary.
        self._multi_sid = multi_sid and bool(getattr(api.profile, "supports_config", False))
        self._max_sids = MAX_SIDS if max_sids is None else max(1, min(max_sids, MAX_SIDS))

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
        # Highest chip index seen on the wire (reader thread); process_frame
        # compares against _active_chips to trigger a live remap on the main
        # thread (avoids mutating display state from the reader).
        self._max_chip_seen = 0
        # Snapshot of the SID-address config taken before the first remap, for
        # restore on teardown. None until a remap happens.
        self._saved_config: dict[tuple[str, str], str] | None = None
        self._socket_present = (False, False)

        self._midi_port = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dirty = True  # force first text-row paint

    # ---- MIDI plumbing -------------------------------------------------------
    def _open_port(self):
        assert mido is not None
        if self.port_name in (None, "", "default"):
            names = mido.get_input_names()
            if not names:
                raise RuntimeError("AsidScene: no MIDI input ports available")
            self._midi_port = mido.open_input(names[0])
            log.info("AsidScene: opened MIDI port %r", names[0])
            return
        # Partial (case-insensitive substring) matching so users don't need the
        # exact rtmidi string.
        names = mido.get_input_names()
        match = next((n for n in names if self.port_name.lower() in n.lower()), None)
        if match is None:
            raise RuntimeError(
                f"AsidScene: no MIDI input port matches {self.port_name!r}; available: {names}"
            )
        self._midi_port = mido.open_input(match)
        log.info("AsidScene: opened MIDI port %r", match)

    def _reader(self):
        port = self._midi_port
        if port is None:
            return
        # Drain all pending SysEx each pass into the per-chip register shadows,
        # then flush coalesced block writes at a bounded rate. An ASID host
        # sends up to ~50-60 frames/sec (higher for multispeed tunes), each
        # touching most of the 25 registers of one or more chips; applying every
        # frame as its own DMA burst would outrun the U64's write ceiling and
        # drop the socket. Coalescing to the latest shadows keeps the link
        # healthy (a multispeed tune loses some intermediate frames — a known
        # v1 limitation).
        last_flush = 0.0
        try:
            while not self._stop.is_set():
                for msg in port.iter_pending():
                    if msg.type == "sysex":
                        self._handle_sysex(msg.data)
                now = time.time()
                if self._pending_flush and now - last_flush >= _FLUSH_INTERVAL_S:
                    self._flush_to_sid()
                    last_flush = now
                time.sleep(0.001)  # 1 ms poll
        except Exception:
            log.exception("AsidScene reader crashed")

    def _chip_for(self, chip_index: int) -> int:
        """Map an ASID chip index to the shadow/emulator slot we use.

        Multi-SID enabled: the index itself, clamped to the max we can host
        (the surplus is dropped once, warned). Disabled: everything downmixes
        to the primary SID (slot 0) with a one-time warning."""
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

        Runs on the reader thread. The per-chip register shadows are only ever
        touched here (single-threaded), so they need no lock; the emulators
        (shared with the render + envelope threads) are guarded in
        _flush_to_sid."""
        update = asid.decode(data)
        if update is None:
            return  # foreign SysEx — not ASID
        if update.dropped:
            if update.command not in self._warned_cmds:
                self._warned_cmds.add(update.command)
                log.warning(
                    "AsidScene: ignoring unsupported ASID command 0x%02X (OPL-FM / timing)",
                    update.command,
                )
            return
        if update.regs:
            chip = self._chip_for(update.chip_index)
            shadow = self._sid_shadows[chip]
            for offset, value in update.regs.items():
                shadow[offset] = value & 0xFF
            if update.control_first:
                cf = self._pending_ctrl_first.setdefault(chip, {})
                for voice, fval in update.control_first.items():
                    cf[voice] = fval
            self._dirty_chips.add(chip)
            self._pending_flush = True
            if self._multi_sid and update.chip_index > self._max_chip_seen:
                # Grow handled on the main thread (process_frame) to keep display
                # mutation off the reader thread.
                self._max_chip_seen = min(update.chip_index, self._max_sids - 1)
        if update.text is not None:
            self._status_text = update.text
            self._dirty = True
        if update.playing is not None and update.playing != self._playing:
            self._playing = update.playing
            self._dirty = True
        if update.system is not None and update.system != self.system:
            self.system = update.system
            with self._reg_lock:
                for emu in self._emulators:
                    emu.clock = CLOCK_NTSC if update.system == "NTSC" else CLOCK_PAL
            self._dirty = True
        if (
            update.chip_index == 0
            and update.chip_type is not None
            and update.chip_type != self._chip_type
        ):
            self._chip_type = update.chip_type
            self._dirty = True

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
    def _detect_sockets(self) -> tuple[bool, bool]:
        """Read which physical SID sockets carry a detected chip (best-effort)."""
        try:
            sockets = self.api.get_config_category(CAT_SOCKETS)
        except Exception:
            log.debug("AsidScene: socket detection read failed", exc_info=True)
            return (False, False)
        s1 = sockets.get(ITEM_SOCKET1_TYPE, "None") not in ("None", "")
        s2 = sockets.get(ITEM_SOCKET2_TYPE, "None") not in ("None", "")
        return (s1, s2)

    def _snapshot_config(self) -> None:
        """Snapshot the SID-address config we're about to change, once, so
        teardown can restore it (best-effort)."""
        if self._saved_config is not None:
            return
        saved: dict[tuple[str, str], str] = {}
        try:
            addressing = self.api.get_config_category(CAT_ADDRESSING)
            sockets = self.api.get_config_category(CAT_SOCKETS)
        except Exception:
            log.debug("AsidScene: config snapshot read failed", exc_info=True)
            self._saved_config = {}  # nothing to restore
            return
        for item in _MANAGED_ADDRESSING_ITEMS:
            if item in addressing:
                saved[(CAT_ADDRESSING, item)] = addressing[item]
        for item in _MANAGED_SOCKET_ITEMS:
            if item in sockets:
                saved[(CAT_SOCKETS, item)] = sockets[item]
        self._saved_config = saved

    def _reconfigure_chips(self, n: int) -> None:
        """Grow the active SID map to `n` chips: configure the U64 address map
        live, update routing, and reflow the split scope. Runs on the main
        (render) thread from process_frame."""
        n = max(1, min(n, self._max_sids))
        self._snapshot_config()
        sid_map = plan_sid_map(
            n, socket1_present=self._socket_present[0], socket2_present=self._socket_present[1]
        )
        for (category, item), value in sid_map.config.items():
            try:
                self.api.put_config_item(category, item, value)
            except Exception:
                log.warning(
                    "AsidScene: failed to set %s/%s=%s", category, item, value, exc_info=True
                )
        self._chip_addresses = list(sid_map.addresses)
        self._active_chips = sid_map.n
        log.info(
            "AsidScene: mapped %d SID chip(s) → %s",
            sid_map.n,
            ", ".join(f"${a:04X}" for a in sid_map.addresses),
        )
        # Reflow the split scope: new window count, then a full bitmap bring-up
        # to clear the old windows' pixels and repaint idle strips + info rows.
        self._set_window_count(sid_map.n)
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        self._window_sounding = [[False] * MAX_SIDS for _ in range(SID.N_VOICES)]
        self._last_window_wave = [[-1] * MAX_SIDS for _ in range(SID.N_VOICES)]
        for v in range(SID.N_VOICES):
            self._paint_strip_color_row(v, [C64_COLORS[_IDLE_GRAY]] * self._n_windows)
        self._alloc_scope_buffers()
        self._dirty = True

    def _restore_config(self) -> None:
        if not self._saved_config:
            return
        for (category, item), value in self._saved_config.items():
            try:
                self.api.put_config_item(category, item, value)
            except Exception:
                log.debug(
                    "AsidScene: config restore of %s/%s failed", category, item, exc_info=True
                )

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

    def _paint_info_rows(self) -> None:
        title_fg = C64_COLORS.get(TITLE_TEXT_COLOR, C64_COLORS["white"])
        self._paint_text_row(
            TITLE_ROW,
            self._build_title_line(),
            title_fg,
            RegionID.WAVE_TITLE_BITMAP,
            RegionID.WAVE_TITLE_SCREEN,
        )
        meta_fg = C64_COLORS.get(METADATA_TEXT_COLOR, C64_COLORS["light gray"])
        self._paint_text_row(
            META_ROW,
            self._build_meta_line(),
            meta_fg,
            RegionID.WAVE_META_BITMAP,
            RegionID.WAVE_META_SCREEN,
        )

    # ---- Scene lifecycle -----------------------------------------------------
    def setup(self) -> None:
        super().setup()
        # Bitmap bring-up: invalidate the delta cache (previous scene may have
        # used $0400/$2000 for char content), engage hires, paint idle strips +
        # info rows, allocate render buffers, then start the MIDI reader +
        # envelope ticker. No SID pre-programming — the ASID stream sets it all.
        if self._multi_sid:
            self._socket_present = self._detect_sockets()
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        self._window_sounding = [[False] * MAX_SIDS for _ in range(SID.N_VOICES)]
        self._last_window_wave = [[-1] * MAX_SIDS for _ in range(SID.N_VOICES)]
        for idx in range(SID.N_VOICES):
            self._repaint_voice_color(idx, C64_COLORS[_IDLE_GRAY])
        self._paint_info_rows()
        self._alloc_scope_buffers()
        self._open_port()
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._reader, daemon=True, name="asid-reader")
        self._reader_thread.start()
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
        with self._reg_lock:
            states = [
                [
                    (
                        v.gated() or v.envelope_level > _ENV_SILENCE_EPS,
                        primary_waveform(v.control),
                    )
                    for v in self._emulators[c].voices
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
                    self._voice_color_now(v_idx, self._emulators[c])
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
        super().teardown()
        self._stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        if self._midi_port is not None:
            try:
                self._midi_port.close()
            except Exception:
                log.debug("AsidScene: port close failed", exc_info=True)
            self._midi_port = None
        if self._poll is not None:
            self._poll.stop()
            self._poll = None
        # Silence every mapped SID, restore the SID-address config we changed,
        # then restore VIC bank 0 + default $D018 for the next scene's char mode.
        try:
            for base in self._chip_addresses:
                if base != SID.BASE:
                    self.api.write_regs(f"{base:04X}", *bytes(SID_REG_COUNT))
            self.api.silence_sid()
            self._restore_config()
            self.api.write_memory(f"{CIA2.PORT_A:04X}", f"{CIA2.PORT_A_BANK_0:02X}")
            self.api.write_memory("d018", f"{D018_HIRES_BITMAP:02X}")
            self.api.flush()
        except Exception:
            log.debug("AsidScene: teardown silence/restore failed", exc_info=True)
