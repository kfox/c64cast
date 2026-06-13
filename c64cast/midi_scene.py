"""MIDI → SID scene. Drives the C64's SID directly from incoming MIDI.

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

Visualization is intentionally minimal: a PETSCII status block shows
the current note and envelope phase per voice. For something flashier,
attach overlays (spectrum_petscii, scrolling_text, etc.) to the scene.

Requires the `midi` extra (``pip install c64cast[midi]``).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .c64 import SID, RegionID, cpu_clock
from .modes import PETSCIIDisplayMode
from .overlays import ascii_to_screen
from .palette import C64_COLORS
from .scenes import Scene

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


class MidiScene(Scene):
    """Open a MIDI input and play notes through the SID's three voices.

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
                 system: str = "NTSC",
                 name: str = "MIDI"):
        super().__init__(api, audio, PETSCIIDisplayMode(), name)
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

        # Default voice colors land somewhere C64-friendly.
        default_colors = ["light green", "cyan", "yellow"]
        if voice_colors is None:
            voice_colors = default_colors
        if len(voice_colors) < SID.N_VOICES:
            voice_colors = (
                list(voice_colors)
                + default_colors[len(voice_colors):]
            )[:SID.N_VOICES]
        self.voice_colors = [
            C64_COLORS.get(c, C64_COLORS["white"]) for c in voice_colors
        ]

        self.voices: list[_VoiceState] = [
            _VoiceState() for _ in range(SID.N_VOICES)
        ]
        self._allocation_lock = threading.Lock()
        self._midi_port = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dirty = True   # force first paint
        # The status block is a low-rate text readout, not animation. Cap
        # its repaint well below the 60 fps system default so per-note
        # screen pushes don't burst the shared DMA socket during fast
        # playing (which can trip a reconnect stall). Note→sound latency is
        # unaffected — notes are written from the MIDI reader thread, not
        # this render loop.
        self.target_fps = 20.0

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
        elif cc == 74:
            self.filter_cutoff = (value << 4) & 0x07FF
            self.api.write_regs(
                f"{SID.FC_LO:04X}",
                self.filter_cutoff & 0x07,
                (self.filter_cutoff >> 3) & 0xFF,
            )

    def _pitchwheel(self, pitch: int) -> None:
        # Re-emit frequency for every gated voice with a +/- 2 semitone bend.
        bend_semitones = (pitch / 8192.0) * 2.0
        for idx, v in enumerate(self.voices):
            if v.on and v.note is not None:
                freq = _note_to_sid_freq(
                    v.note + bend_semitones, self.system)
                base = SID.voice_base(idx)
                self.api.write_regs(
                    f"{base + SID.OFF_FREQ_LO:04X}",
                    freq & 0xFF, (freq >> 8) & 0xFF,
                )

    # ---- SID writes ----------------------------------------------------------
    def _program_voice(self, voice_idx: int, midi_note: int, gate: bool) -> None:
        base = SID.voice_base(voice_idx)
        freq = _note_to_sid_freq(midi_note, self.system)
        # Coalesce freq + pulse-width + control + ADSR (7 contiguous bytes)
        # into a single PUT — minimises HTTP overhead per note event.
        ctrl = self.waveform_bits | (SID.GATE if gate else 0)
        a, d, s, r = self.adsr
        self.api.write_regs(
            f"{base:04X}",
            freq & 0xFF,
            (freq >> 8) & 0xFF,
            self.pulse_width & 0xFF,
            (self.pulse_width >> 8) & 0x0F,
            ctrl,
            ((a & 0xF) << 4) | (d & 0xF),
            ((s & 0xF) << 4) | (r & 0xF),
        )

    def _program_global_sid(self) -> None:
        # Filter routing: low nibble of $D417 selects which voices feed
        # the filter. Default: route nothing through the filter so notes
        # are audible even at FC=0.
        self.api.write_memory(f"{SID.RES_FILT:04X}", "00")
        fc = self.filter_cutoff
        self.api.write_regs(
            f"{SID.FC_LO:04X}",
            fc & 0x07,
            (fc >> 3) & 0xFF,
        )
        # Mode bits in high nibble of $D418: lp=1, bp=2, hp=4, mute_v3=8.
        mode_bits = {"lowpass": 0x1, "bandpass": 0x2, "highpass": 0x4}.get(
            self.filter_mode, 0x1)
        self.api.write_memory(
            f"{SID.MODE_VOL:04X}",
            f"{((mode_bits & 0xF) << 4) | (self.master_volume & 0xF):02X}",
        )

    # ---- Scene lifecycle -----------------------------------------------------
    def setup(self) -> None:
        super().setup()
        self._program_global_sid()
        # Pre-program ADSR + waveform on all voices so a note_on only
        # needs to write freq + gate.
        for vidx in range(SID.N_VOICES):
            base = SID.voice_base(vidx)
            a, d, s, r = self.adsr
            self.api.write_regs(
                f"{base + SID.OFF_PW_LO:04X}",
                self.pulse_width & 0xFF,
                (self.pulse_width >> 8) & 0x0F,
                self.waveform_bits,      # gate off
                ((a & 0xF) << 4) | (d & 0xF),
                ((s & 0xF) << 4) | (r & 0xF),
            )
        self._open_port()
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name="midi-reader")
        self._reader_thread.start()
        self._dirty = True

    def process_frame(self, current_time: float) -> bool:
        if not self._dirty:
            return True
        chars = bytearray([0x20] * 1000)   # space (screen code)
        colors = bytearray([C64_COLORS["dark gray"]] * 1000)

        # Header row: scene name + waveform + master volume.
        header = (
            f"MIDI  {self.waveform.upper()[:4]:4s}  "
            f"VOL {self.master_volume:2d}"
        )[:40]
        self._paint(chars, colors, row=0, col=0,
                    text=header, color=C64_COLORS["white"])

        # One row per voice. Only gated (sounding) voices show a note +
        # velocity; a released voice clears back to "--- off" so the
        # display reflects what's actually being heard rather than the
        # last note that played. (chars is rebuilt from spaces each frame,
        # so the shorter released line overwrites the stale text.) Released
        # rows are dimmed to read as inactive.
        for i, v in enumerate(self.voices):
            row = 10 + i * 2
            note = v.note
            if v.on and note is not None:
                line = f"V{i + 1}  {_note_name(note):3s}  ON   vel {v.velocity:3d}"
                color = self.voice_colors[i]
            else:
                line = f"V{i + 1}  ---  off"
                color = C64_COLORS["dark gray"]
            self._paint(chars, colors, row=row, col=2,
                        text=line[:36], color=color)

        self.api.write_region(0x0400, bytes(chars), region_id=RegionID.SCREEN)
        self.api.write_region(0xD800, bytes(colors), region_id=RegionID.COLOR)
        self._dirty = False
        return True

    def _paint(self, chars: bytearray, colors: bytearray,
               row: int, col: int, text: str, color: int) -> None:
        if not 0 <= row < 25:
            return
        encoded = ascii_to_screen(text)
        start = row * 40 + col
        end = min(start + len(encoded), row * 40 + 40)
        chars[start:end] = encoded[: end - start]
        for i in range(start, end):
            colors[i] = color

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
        # Silence the SID so the next scene starts clean.
        try:
            self.api.silence_sid()
        except Exception:
            log.debug("MidiScene: silence_sid failed", exc_info=True)
