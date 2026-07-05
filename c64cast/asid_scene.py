"""ASID → SID scene. Plays an incoming ASID MIDI stream on the real SID and
visualizes the three voices as a full-screen hires oscilloscope.

Any ASID *host* — DeepSID (browser), SIDFactory II, Plogue chipsynth C64,
Elektron ASID-XP — streams packed SID register writes over MIDI SysEx; this
scene decodes them (see :mod:`c64cast.asid`), writes them to the real SID chip
over DMA, and drives the shared 3-voice oscilloscope. It turns c64cast into an
ASID *client* whose SID happens to be genuine hardware on the U64/TeensyROM,
with the scope on HDMI.

This is the sibling of :class:`~c64cast.midi_scene.MidiScene`: same
:class:`~c64cast.voice_scope.VoiceScopeRenderer` visualization, same MIDI-port
plumbing, same 25-byte ``$D400-$D418`` register shadow feeding a host-side
:class:`~c64cast.sidemu.SIDEmulator`. The difference is the input: MidiScene
*synthesizes* SID writes from notes/CCs, while AsidScene receives the finished
register bytes and just relays them — so all the synth knobs (waveform, ADSR,
filter, voice allocation) are gone; ASID carries that state itself.

Register frames are coalesced and flushed to the SID at a bounded rate (see
``_FLUSH_INTERVAL_S``) so a burst — or a high-multispeed tune — can't outrun
the DMA socket. Each flush is a single ``$D400-$D418`` block write; a within-
frame gate-off→gate-on "hard restart" additionally emits the first control
value just before the block so the pulse reaches the chip.

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


class AsidScene(VoiceScopeRenderer, Scene):
    """Receive an ASID MIDI stream and play it on the real SID + oscilloscope
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

        # Host-side SID model driving the oscilloscope, fed from our register
        # shadow. The poll thread advances ADSR envelopes at the video rate.
        self.emulator = SIDEmulator(system=system)
        self._reg_lock = threading.Lock()
        self._sid_shadow = bytearray(SID_REG_COUNT)  # mirrors $D400-$D418
        self._video_hz = 50.0 if system.upper() == "PAL" else 60.0
        self._poll_dt = 1.0 / self._video_hz
        self._poll: PollThread | None = None

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
        )

        # Per-voice display state, change-detected in process_frame.
        self._voice_sounding: list[bool] = [False, False, False]
        self._last_voice_wave: list[int] = [-1, -1, -1]

        # ASID stream state.
        self._playing: bool = False
        self._status_text: str = ""  # latest 0x4F display text
        self._chip_type: str | None = None
        # Pending register flush: the reader thread accumulates into the shadow
        # + control-first map and flushes at _FLUSH_INTERVAL_S.
        self._pending_flush = False
        self._pending_ctrl_first: dict[int, int] = {}
        self._warned_cmds: set[int] = set()

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
        # Drain all pending SysEx each pass into the register shadow, then flush
        # a coalesced block write at a bounded rate. An ASID host sends up to
        # ~50-60 frames/sec (higher for multispeed tunes), each touching most
        # of the 25 registers; applying every frame as its own DMA burst would
        # outrun the U64's write ceiling and drop the socket. Coalescing to the
        # latest shadow keeps the link healthy (a multispeed tune loses some
        # intermediate frames — a known v1 limitation).
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

    def _handle_sysex(self, data) -> None:
        """Decode one ASID SysEx message and fold it into the pending state.

        Runs on the reader thread. The register shadow is only ever touched
        here (single-threaded), so it needs no lock; the emulator (shared with
        the render + envelope threads) is guarded in _flush_to_sid."""
        update = asid.decode(data)
        if update is None:
            return  # foreign SysEx — not ASID
        if update.dropped:
            if update.command not in self._warned_cmds:
                self._warned_cmds.add(update.command)
                log.warning(
                    "AsidScene: ignoring unsupported ASID command 0x%02X "
                    "(multi-SID / OPL-FM / timing are single-SID-only in v1)",
                    update.command,
                )
            return
        if update.regs:
            for offset, value in update.regs.items():
                self._sid_shadow[offset] = value & 0xFF
            for voice, fval in update.control_first.items():
                self._pending_ctrl_first[voice] = fval
            self._pending_flush = True
        if update.text is not None:
            self._status_text = update.text
            self._dirty = True
        if update.playing is not None and update.playing != self._playing:
            self._playing = update.playing
            self._dirty = True
        if update.system is not None and update.system != self.system:
            self.system = update.system
            with self._reg_lock:
                self.emulator.clock = CLOCK_NTSC if update.system == "NTSC" else CLOCK_PAL
            self._dirty = True
        if update.chip_type is not None and update.chip_type != self._chip_type:
            self._chip_type = update.chip_type
            self._dirty = True

    def _flush_to_sid(self) -> None:
        """Write the accumulated register shadow to the real SID (reader thread).

        Hard-restart first-writes go out as individual writes *before* the
        coalesced block (which lands the second/final control value), so the
        gate-off pulse reaches the chip. The same case is invisible to the
        emulator's gate-edge detection (the shadow's prior control byte was
        gated), so flag it via `retrigger`."""
        retrigger: tuple[bool, ...] | None = None
        if self._pending_ctrl_first:
            for voice, fval in self._pending_ctrl_first.items():
                addr = SID.voice_base(voice) + SID.OFF_CONTROL
                self.api.write_memory(f"{addr:04X}", f"{fval & 0xFF:02X}")
            mask = [False] * SID.N_VOICES
            for voice in self._pending_ctrl_first:
                mask[voice] = True
            retrigger = tuple(mask)
            self._pending_ctrl_first = {}
        # One coalesced block write of the whole 25-byte $D400-$D418 image.
        self.api.write_regs(f"{SID.BASE:04X}", *self._sid_shadow)
        with self._reg_lock:
            self.emulator.update_registers(bytes(self._sid_shadow), retrigger=retrigger)
        self._pending_flush = False

    # ---- info text rows ------------------------------------------------------
    def _build_title_line(self) -> str:
        """Scene name (left), play state + chip type (right)."""
        state = "PLAYING" if self._playing else "READY"
        if self._chip_type:
            state = f"{self._chip_type} {state}"
        return _layout_lr(self.name[:24], state)

    def _build_meta_line(self) -> str:
        """The latest 0x4F display text if the host sent any, else a per-voice
        waveform + master-volume summary derived from the register shadow."""
        if self._status_text:
            return self._status_text[:40].ljust(40)
        with self._reg_lock:
            waves = [primary_waveform(v.control) for v in self.emulator.voices]
        tags = " ".join(f"{i + 1}:{_WAVE_ABBREV.get(w, '---')}" for i, w in enumerate(waves))
        vol = self._sid_shadow[_MODE_VOL_OFFSET] & 0x0F
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
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        self._voice_sounding = [False, False, False]
        self._last_voice_wave = [-1, -1, -1]
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
            self.emulator.advance_envelopes(self._poll_dt)

    def process_frame(self, current_time: float) -> bool:
        # Activity coloring: a sounding voice (gated or still decaying) draws in
        # its color; an idle voice fades to gray. Change-detected so the screen
        # color write only fires on a transition.
        with self._reg_lock:
            states = [
                (v.gated() or v.envelope_level > _ENV_SILENCE_EPS, primary_waveform(v.control))
                for v in self.emulator.voices
            ]
        for i, (sounding, wave) in enumerate(states):
            changed = sounding != self._voice_sounding[i] or (
                sounding and wave != self._last_voice_wave[i]
            )
            if changed:
                color = self._voice_color_now(i) if sounding else C64_COLORS[_IDLE_GRAY]
                self._repaint_voice_color(i, color)
                self._voice_sounding[i] = sounding
                self._last_voice_wave[i] = wave
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
        # Silence the SID, then restore VIC bank 0 + default $D018 so the next
        # scene's char-mode display renders cleanly.
        try:
            self.api.silence_sid()
            self.api.write_memory(f"{CIA2.PORT_A:04X}", f"{CIA2.PORT_A_BANK_0:02X}")
            self.api.write_memory("d018", f"{D018_HIRES_BITMAP:02X}")
            self.api.flush()
        except Exception:
            log.debug("AsidScene: teardown silence/restore failed", exc_info=True)
