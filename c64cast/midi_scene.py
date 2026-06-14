"""MIDI → SID scene. Drives the C64's SID directly from incoming MIDI, and
visualizes the three voices as a full-screen hires oscilloscope.

A live MIDI input port (any source — keyboard, DAW, looper) becomes a
real-time control surface for the SID's three voices. Note on/off
events are mapped to voice frequency + gate; pitch-bend and a few CC
numbers map to common SID parameters (filter cutoff, pulse width,
master volume).

This is the "play the C64 like a $2 synth" path. The audio NMI loop is
NOT used — the SID's native voices generate the sound. If the playlist
has [audio] enabled, this scene leaves it alone; the audio loop touches
only the $D418 volume nibble and the MIDI scene reserves writes to
that register for its own master-volume CC. (If you have both, the
last writer wins.)

Visualization is the shared :class:`~c64cast.voice_scope.VoiceScopeRenderer`
oscilloscope (the same one WaveformScene uses): three stacked voice strips
in a 320×200 hires bitmap, with the per-voice readout (note / velocity /
waveform) and the global state (port waveform / master volume) on the two
bottom text rows. Unlike WaveformScene — which mirrors a write-only SID via a
parallel py65 6502 — MidiScene *is* the writer: it keeps a 25-byte $D400-$D418
register shadow updated alongside every SID write and feeds the host-side
:class:`~c64cast.sidemu.SIDEmulator` directly. A light background poll thread
advances the ADSR envelopes at the video rate so attack/decay/release tails
evolve on screen between MIDI events.

Display is bitmap-only, so PETSCII overlays don't apply (overlay-compat
rejects them against the hires mode `_validate_midi` reports).

Requires the `midi` extra (``pip install c64cast[midi]``).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ._pollthread import PollThread
from .c64 import CIA2, SID, VIC_BANK_0, RegionID, cpu_clock
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

# Typed as Any so Pyright doesn't flag every mido.XXX as accessing attributes
# of None — the MIDI_AVAILABLE flag is the runtime guard. Also sidesteps
# pyright not seeing mido.open_input / mido.get_input_names through stubs.
try:
    import mido as _mido
    mido: Any = _mido
    MIDI_AVAILABLE = True
except ImportError:
    mido = None
    MIDI_AVAILABLE = False


# SID waveform-select control bits (high nibble of voice control register).
_WAVEFORM_BITS = {
    "triangle": SID.WAVE_TRIANGLE,
    "sawtooth": SID.WAVE_SAWTOOTH,
    "pulse":    SID.WAVE_PULSE,
    "noise":    SID.WAVE_NOISE,
}

# Standard MIDI note names. Aligned so note 60 = C-4.
_NOTE_NAMES = ("C-", "C#", "D-", "D#", "E-", "F-",
               "F#", "G-", "G#", "A-", "A#", "B-")

# Mod-wheel (CC1) → pulse-width window. A pulse wave collapses to silent DC
# at both 0% and 100% duty, so the wheel is mapped into an audible window
# rather than the raw 0..4095 register range — wheel-to-zero used to mute
# the voices. Mid-wheel lands near 50% (a square wave).
_PW_MIN_AUDIBLE = 128    # ~3% duty
_PW_MAX_AUDIBLE = 3968   # ~97% duty

# Max rate at which coalesced continuous controllers (pitch bend, mod wheel,
# filter cutoff, volume) are flushed to the SID. 60 Hz is smooth to the ear
# and keeps wheel sweeps from bursting the DMA socket. See _reader().
_CONTROL_FLUSH_INTERVAL_S = 1.0 / 60.0


def _note_name(midi_note: int) -> str:
    n = max(0, min(127, int(midi_note)))
    return f"{_NOTE_NAMES[n % 12]}{(n // 12) - 1:1d}"


def _note_to_sid_freq(midi_note: float, system: str) -> int:
    """Convert a MIDI note number (fractional allowed for pitch-bend) to a
    16-bit SID frequency register value for the given system clock.

    Formula from the SID datasheet: freq_reg = freq_hz * 16777216 / clock.
    """
    hz = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
    clock = cpu_clock(system)
    return max(0, min(0xFFFF, int(hz * 16777216 / clock)))


class _VoiceState:
    """Lightweight bookkeeping for one of the SID's three voices."""

    __slots__ = ("note", "on", "t_changed", "velocity")

    def __init__(self) -> None:
        self.note: int | None = None
        self.on: bool = False
        self.t_changed: float = 0.0
        self.velocity: int = 0


class MidiScene(VoiceScopeRenderer, Scene):
    """Open a MIDI input and play notes through the SID's three voices,
    visualized as a hires oscilloscope (see the module docstring).

    Voice allocation is round-robin with voice-stealing: when all three
    voices are gated and a new note arrives, the oldest one is dropped
    and replaced. Per-voice waveform / ADSR / pulse width come from the
    scene config and are programmed once at setup.
    """

    WANTS_AUDIO_LOCK = True

    def __init__(self, api, audio, port: str | None = None,
                 waveform: str = "pulse",
                 adsr: tuple[int, int, int, int] = (0, 8, 12, 8),
                 pulse_width: int = 2048,
                 filter_cutoff: int = 1024,
                 filter_mode: str = "lowpass",
                 master_volume: int = 15,
                 voice_colors: list[str] | None = None,
                 color_mode: str = "per_voice",
                 waveform_colors: dict | None = None,
                 time_base: str = "wallclock",
                 auto_cycles: float = 4.0,
                 persistence: str = "off",
                 scroll_columns: int | list[int] = 0,
                 target_fps: float | None = None,
                 system: str = "NTSC",
                 name: str = "MIDI"):
        super().__init__(api, audio, None, name)
        if not MIDI_AVAILABLE:
            raise RuntimeError(
                "MidiScene requires mido + python-rtmidi "
                "(pip install c64cast[midi])"
            )
        if waveform not in _WAVEFORM_BITS:
            raise ValueError(
                f"MidiScene: waveform must be one of {sorted(_WAVEFORM_BITS)}, "
                f"got {waveform!r}"
            )
        if len(adsr) != 4 or not all(0 <= v <= 15 for v in adsr):
            raise ValueError(
                "MidiScene: adsr must be 4 ints in 0..15, got "
                f"{adsr!r}"
            )
        if not 0 <= pulse_width <= 4095:
            raise ValueError("MidiScene: pulse_width must be 0..4095")
        if not 0 <= filter_cutoff <= 2047:
            raise ValueError("MidiScene: filter_cutoff must be 0..2047 (11-bit)")
        if not 0 <= master_volume <= 15:
            raise ValueError("MidiScene: master_volume must be 0..15")

        self.port_name = port
        self.waveform = waveform
        self.waveform_bits = _WAVEFORM_BITS[waveform]
        self.adsr = tuple(adsr)
        self.pulse_width = int(pulse_width)
        self.filter_cutoff = int(filter_cutoff)
        self.filter_mode = filter_mode
        self.master_volume = int(master_volume)
        self.system = system

        # Voice trace colors. Pad the configured names to 3 with C64-friendly
        # defaults; the scope mixin (via _init_scope_knobs) resolves these to
        # palette indices and requires at least 3.
        default_colors = ["light green", "cyan", "yellow"]
        names = list(voice_colors) if voice_colors else list(default_colors)
        if len(names) < SID.N_VOICES:
            names = (names + default_colors[len(names):])[:SID.N_VOICES]

        # Bitmap display: fixed VIC bank 0 ($0400 screen / $2000 bitmap). No
        # relocation — MidiScene uploads no SID payload and leaves the audio
        # ring idle, so bank 0's display regions are always free.
        self._screen_base = VIC_BANK_0.SCREEN
        self._bitmap_base = VIC_BANK_0.BITMAP
        self._dd00 = CIA2.PORT_A_BANK_0
        self._d018 = D018_HIRES_BITMAP

        # Host-side SID model driving the oscilloscope. We feed it from our own
        # $D400-$D418 register shadow (below) rather than a py65 host emulator —
        # MidiScene computes every SID byte it sends, so no parallel 6502 is
        # needed. The poll thread advances the ADSR envelopes at the video rate.
        self.emulator = SIDEmulator(system=system)
        self._reg_lock = threading.Lock()
        self._sid_shadow = bytearray(SID_REG_COUNT)   # mirrors $D400-$D418
        self._video_hz = 50.0 if system.upper() == "PAL" else 60.0
        self._poll_dt = 1.0 / self._video_hz
        self._poll: PollThread | None = None

        # Default to HALF the system video rate (30 NTSC / 25 PAL), matching
        # WaveformScene: an oscilloscope reads fine at half-rate and it halves
        # the per-frame bitmap DMA volume. The text rows are change-detected
        # (repainted only on note/CC events), so they add little. An explicit
        # target_fps (CLI/TOML) still wins. The envelope poll rate is
        # independent (self._video_hz) and stays at the full video rate.
        if target_fps is None:
            target_fps = self._video_hz / 2.0
        self.target_fps = float(target_fps)

        # Scope visualization knobs (validates color_mode/time_base/
        # auto_cycles/persistence/scroll_columns; sets the render modes +
        # buffers + frame_time_s). One displayed column-window = one display
        # frame of audio time.
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
        # Track the last waveform per voice so per_waveform color-RAM writes
        # only fire on transitions (see process_frame).
        self._last_voice_wave: list[int] = [-1, -1, -1]

        self.voices: list[_VoiceState] = [
            _VoiceState() for _ in range(SID.N_VOICES)
        ]
        self._allocation_lock = threading.Lock()
        self._midi_port = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dirty = True   # force first text-row paint

    # ---- MIDI plumbing -------------------------------------------------------
    def _open_port(self):
        assert mido is not None
        if self.port_name in (None, "", "default"):
            names = mido.get_input_names()
            if not names:
                raise RuntimeError("MidiScene: no MIDI input ports available")
            self._midi_port = mido.open_input(names[0])
            log.info("MidiScene: opened MIDI port %r", names[0])
            return
        # Allow partial-name matching so users don't need to paste the
        # exact rtmidi string.
        names = mido.get_input_names()
        match = next(
            (n for n in names if self.port_name.lower() in n.lower()), None)
        if match is None:
            raise RuntimeError(
                f"MidiScene: no MIDI input port matches "
                f"{self.port_name!r}; available: {names}")
        self._midi_port = mido.open_input(match)
        log.info("MidiScene: opened MIDI port %r", match)

    def _reader(self):
        port = self._midi_port
        if port is None:
            return
        # Continuous controllers (pitch bend, mod/expression wheels) stream
        # dozens-to-hundreds of messages per second while a wheel moves, and
        # each pitch-bend fans out to one SID write per gated voice. Applying
        # every message in a tight drain loop bursts the DMA socket faster
        # than the U64 accepts it, which closes the connection (a "broken
        # pipe" that the socket layer then has to reconnect through). Guard
        # against it: drain all pending messages each pass, coalesce each
        # continuous controller down to its newest value, and flush those at
        # a bounded rate. Notes stay discrete and are applied immediately so
        # attack latency isn't affected.
        pending_pitch: int | None = None
        pending_cc: dict[int, int] = {}
        last_flush = 0.0
        try:
            while not self._stop.is_set():
                for msg in port.iter_pending():
                    if msg.type in ("note_on", "note_off"):
                        self._handle_msg(msg)
                    elif msg.type == "pitchwheel":
                        pending_pitch = msg.pitch
                    elif msg.type == "control_change":
                        pending_cc[msg.control] = msg.value
                now = time.time()
                if ((pending_pitch is not None or pending_cc)
                        and now - last_flush >= _CONTROL_FLUSH_INTERVAL_S):
                    for ctrl, val in pending_cc.items():
                        self._control_change(ctrl, val)
                    pending_cc = {}
                    if pending_pitch is not None:
                        self._pitchwheel(pending_pitch)
                        pending_pitch = None
                    last_flush = now
                time.sleep(0.001)   # 1 ms poll — keeps note latency tight
        except Exception:
            log.exception("MidiScene reader crashed")

    def _handle_msg(self, msg) -> None:
        if msg.type == "note_on" and msg.velocity > 0:
            self._note_on(msg.note, msg.velocity)
        elif msg.type in ("note_off",) or (
            msg.type == "note_on" and msg.velocity == 0
        ):
            self._note_off(msg.note)
        elif msg.type == "control_change":
            self._control_change(msg.control, msg.value)
        elif msg.type == "pitchwheel":
            self._pitchwheel(msg.pitch)

    # ---- voice allocation ----------------------------------------------------
    def _pick_voice(self, midi_note: int) -> int:
        # If this note is already playing somewhere, reuse that voice (so
        # consecutive trigger-style note_ons don't waste a slot).
        for i, v in enumerate(self.voices):
            if v.on and v.note == midi_note:
                return i
        # Prefer a released voice.
        for i, v in enumerate(self.voices):
            if not v.on:
                return i
        # Steal the oldest gated voice.
        oldest = min(range(len(self.voices)),
                     key=lambda i: self.voices[i].t_changed)
        return oldest

    def _note_on(self, midi_note: int, velocity: int) -> None:
        now = time.time()
        with self._allocation_lock:
            idx = self._pick_voice(midi_note)
            v = self.voices[idx]
            v.note = midi_note
            v.on = True
            v.t_changed = now
            v.velocity = velocity
        self._program_voice(idx, midi_note, gate=True)
        self._dirty = True

    def _note_off(self, midi_note: int) -> None:
        now = time.time()
        with self._allocation_lock:
            for idx, v in enumerate(self.voices):
                if v.on and v.note == midi_note:
                    v.on = False
                    v.t_changed = now
                    self._program_voice(idx, midi_note, gate=False)
                    self._dirty = True
                    return

    def _control_change(self, cc: int, value: int) -> None:
        # CC1 (mod wheel) → pulse width sweep. CC7 (volume) → SID master.
        # CC74 (filter cutoff) → SID FC. Everything else ignored.
        if cc == 7:
            self.master_volume = max(0, min(15, value >> 3))
            self.api.write_memory(f"{SID.MODE_VOL:04X}",
                                  f"{self.master_volume & 0x0F:02X}")
            self._poke_shadow(SID.MODE_VOL, self.master_volume & 0x0F)
            self._feed_emulator()
            self._dirty = True   # title row shows VOL nn
        elif cc == 1:
            # Map the wheel into an audible window so wheel-to-zero (or
            # full) doesn't drive the pulse to a silent 0% / 100% duty.
            span = _PW_MAX_AUDIBLE - _PW_MIN_AUDIBLE
            self.pulse_width = _PW_MIN_AUDIBLE + (value * span) // 127
            for vidx in range(SID.N_VOICES):
                base = SID.voice_base(vidx)
                self.api.write_regs(
                    f"{base + SID.OFF_PW_LO:04X}",
                    self.pulse_width & 0xFF,
                    (self.pulse_width >> 8) & 0x0F,
                )
                self._poke_shadow(base + SID.OFF_PW_LO, self.pulse_width & 0xFF)
                self._poke_shadow(base + SID.OFF_PW_HI,
                                  (self.pulse_width >> 8) & 0x0F)
            self._feed_emulator()   # pulse width changes the trace shape
        elif cc == 74:
            self.filter_cutoff = (value << 4) & 0x07FF
            self.api.write_regs(
                f"{SID.FC_LO:04X}",
                self.filter_cutoff & 0x07,
                (self.filter_cutoff >> 3) & 0xFF,
            )
            self._poke_shadow(SID.FC_LO, self.filter_cutoff & 0x07)
            self._poke_shadow(SID.FC_HI, (self.filter_cutoff >> 3) & 0xFF)

    def _pitchwheel(self, pitch: int) -> None:
        # Re-emit frequency for every gated voice with a +/- 2 semitone bend.
        bend_semitones = (pitch / 8192.0) * 2.0
        touched = False
        for idx, v in enumerate(self.voices):
            if v.on and v.note is not None:
                freq = _note_to_sid_freq(
                    v.note + bend_semitones, self.system)
                base = SID.voice_base(idx)
                self.api.write_regs(
                    f"{base + SID.OFF_FREQ_LO:04X}",
                    freq & 0xFF, (freq >> 8) & 0xFF,
                )
                self._poke_shadow(base + SID.OFF_FREQ_LO, freq & 0xFF)
                self._poke_shadow(base + SID.OFF_FREQ_HI, (freq >> 8) & 0xFF)
                touched = True
        if touched:
            self._feed_emulator()

    # ---- SID writes + register shadow ---------------------------------------
    def _poke_shadow(self, addr: int, value: int) -> None:
        """Mirror one $D400-$D418 register write into the 25-byte shadow."""
        self._sid_shadow[addr - SID.BASE] = value & 0xFF

    def _feed_emulator(self, retrigger: tuple[bool, ...] | None = None) -> None:
        """Push the current register shadow into the host SID emulator so the
        oscilloscope reflects the latest writes. `retrigger` forces a per-voice
        hard re-attack the gate-edge logic can't see (re-trigger / voice steal
        on an already-gated voice)."""
        with self._reg_lock:
            self.emulator.update_registers(bytes(self._sid_shadow),
                                           retrigger=retrigger)

    def _program_voice(self, voice_idx: int, midi_note: int, gate: bool) -> None:
        base = SID.voice_base(voice_idx)
        freq = _note_to_sid_freq(midi_note, self.system)
        # Coalesce freq + pulse-width + control + ADSR (7 contiguous bytes)
        # into a single PUT — minimises socket overhead per note event.
        ctrl = self.waveform_bits | (SID.GATE if gate else 0)
        a, d, s, r = self.adsr
        regs = (
            freq & 0xFF,
            (freq >> 8) & 0xFF,
            self.pulse_width & 0xFF,
            (self.pulse_width >> 8) & 0x0F,
            ctrl,
            ((a & 0xF) << 4) | (d & 0xF),
            ((s & 0xF) << 4) | (r & 0xF),
        )
        self.api.write_regs(f"{base:04X}", *regs)

        # Mirror into the shadow + feed the emulator. A note that re-gates a
        # voice already gated (re-trigger or voice steal) shows no off→on edge
        # to update_registers, so flag it for a hard re-attack — otherwise a
        # plucked (sustain=0) voice would flatline after its first decay.
        off = voice_idx * SID.BYTES_PER_VOICE
        prev_ctrl = self._sid_shadow[off + SID.OFF_CONTROL]
        self._sid_shadow[off:off + SID.BYTES_PER_VOICE] = bytes(regs)
        retrigger: tuple[bool, ...] | None = None
        if gate and (prev_ctrl & SID.GATE) and (ctrl & SID.GATE):
            mask = [False] * SID.N_VOICES
            mask[voice_idx] = True
            retrigger = tuple(mask)
        self._feed_emulator(retrigger=retrigger)

    def _program_global_sid(self) -> None:
        # Filter routing: low nibble of $D417 selects which voices feed
        # the filter. Default: route nothing through the filter so notes
        # are audible even at FC=0.
        self.api.write_memory(f"{SID.RES_FILT:04X}", "00")
        self._poke_shadow(SID.RES_FILT, 0)
        fc = self.filter_cutoff
        self.api.write_regs(
            f"{SID.FC_LO:04X}",
            fc & 0x07,
            (fc >> 3) & 0xFF,
        )
        self._poke_shadow(SID.FC_LO, fc & 0x07)
        self._poke_shadow(SID.FC_HI, (fc >> 3) & 0xFF)
        # Mode bits in high nibble of $D418: lp=1, bp=2, hp=4, mute_v3=8.
        mode_bits = {"lowpass": 0x1, "bandpass": 0x2, "highpass": 0x4}.get(
            self.filter_mode, 0x1)
        mode_vol = ((mode_bits & 0xF) << 4) | (self.master_volume & 0xF)
        self.api.write_memory(f"{SID.MODE_VOL:04X}", f"{mode_vol:02X}")
        self._poke_shadow(SID.MODE_VOL, mode_vol)

    def _preprogram_voices(self) -> None:
        """Seed each voice's pulse width + waveform + ADSR (gate off) on the
        SID and in the shadow so a note_on only changes freq + gate and the
        emulator starts from the configured (silent) state."""
        a, d, s, r = self.adsr
        for vidx in range(SID.N_VOICES):
            base = SID.voice_base(vidx)
            self.api.write_regs(
                f"{base + SID.OFF_PW_LO:04X}",
                self.pulse_width & 0xFF,
                (self.pulse_width >> 8) & 0x0F,
                self.waveform_bits,      # gate off
                ((a & 0xF) << 4) | (d & 0xF),
                ((s & 0xF) << 4) | (r & 0xF),
            )
            off = vidx * SID.BYTES_PER_VOICE
            self._poke_shadow(base + SID.OFF_PW_LO, self.pulse_width & 0xFF)
            self._poke_shadow(base + SID.OFF_PW_HI, (self.pulse_width >> 8) & 0x0F)
            self._sid_shadow[off + SID.OFF_CONTROL] = self.waveform_bits
            self._sid_shadow[off + SID.OFF_AD] = ((a & 0xF) << 4) | (d & 0xF)
            self._sid_shadow[off + SID.OFF_SR] = ((s & 0xF) << 4) | (r & 0xF)
        self._feed_emulator()

    # ---- info text rows ------------------------------------------------------
    def _build_title_line(self) -> str:
        """Global state: MIDI + current waveform (left), master volume (right)."""
        return _layout_lr(f"MIDI {self.waveform.upper()}",
                          f"VOL {self.master_volume:2d}")

    def _build_meta_line(self) -> str:
        """Condensed per-voice readout for all three voices on one row:
        `V1 C-4 100  V2 E-4  88  V3 ---`. Gated voices show note + velocity;
        released voices show `---`."""
        parts = []
        for i, v in enumerate(self.voices):
            if v.on and v.note is not None:
                parts.append(f"V{i + 1} {_note_name(v.note)} {v.velocity:3d}")
            else:
                parts.append(f"V{i + 1} ---")
        return "  ".join(parts)[:40].ljust(40)

    def _paint_info_rows(self) -> None:
        title_fg = C64_COLORS.get(TITLE_TEXT_COLOR, C64_COLORS["white"])
        self._paint_text_row(TITLE_ROW, self._build_title_line(), title_fg,
                             RegionID.WAVE_TITLE_BITMAP,
                             RegionID.WAVE_TITLE_SCREEN)
        meta_fg = C64_COLORS.get(METADATA_TEXT_COLOR, C64_COLORS["light gray"])
        self._paint_text_row(META_ROW, self._build_meta_line(), meta_fg,
                             RegionID.WAVE_META_BITMAP,
                             RegionID.WAVE_META_SCREEN)

    # ---- Scene lifecycle -----------------------------------------------------
    def setup(self) -> None:
        super().setup()
        self._program_global_sid()
        self._preprogram_voices()
        # Bitmap bring-up: clear + colors + charset, then the two info rows,
        # then allocate the per-voice render buffers. invalidate_cache first so
        # the delta cache gets a clean baseline (the previous scene may have
        # used $0400/$2000 for char-mode content).
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        self._paint_info_rows()
        self._alloc_scope_buffers()
        self._open_port()
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name="midi-reader")
        self._reader_thread.start()
        # Envelope ticker: advances each voice's ADSR at the video rate so
        # attack/decay/release tails evolve on screen between MIDI events.
        self._poll = PollThread(self._tick_envelopes, period=self._poll_dt,
                                name="midi-env")
        self._poll.start()
        self._dirty = True

    def _tick_envelopes(self) -> None:
        with self._reg_lock:
            self.emulator.advance_envelopes(self._poll_dt)

    def process_frame(self, current_time: float) -> bool:
        # Per-waveform coloring: repaint a voice's strip color only when its
        # selected waveform changes (change-detected so normal frames are free).
        if self.color_mode == "per_waveform":
            with self._reg_lock:
                controls = [v.control for v in self.emulator.voices]
            for v_idx in range(SID.N_VOICES):
                wave_now = primary_waveform(controls[v_idx])
                if wave_now != self._last_voice_wave[v_idx]:
                    self._repaint_voice_color(v_idx)
                    self._last_voice_wave[v_idx] = wave_now
        # Text rows repaint only on note/CC changes (keeps DMA low).
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
                log.debug("MidiScene: port close failed", exc_info=True)
            self._midi_port = None
        if self._poll is not None:
            self._poll.stop()
            self._poll = None
        # Silence the SID, then restore VIC bank 0 + the default $D018 so the
        # next scene's char-mode display renders cleanly (we left VIC in hires
        # bitmap mode).
        try:
            self.api.silence_sid()
            self.api.write_memory(f"{CIA2.PORT_A:04X}",
                                  f"{CIA2.PORT_A_BANK_0:02X}")
            self.api.write_memory("d018", f"{D018_HIRES_BITMAP:02X}")
            self.api.flush()
        except Exception:
            log.debug("MidiScene: teardown silence/restore failed",
                      exc_info=True)
