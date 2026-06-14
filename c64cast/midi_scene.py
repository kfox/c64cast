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
    "pulse": SID.WAVE_PULSE,
    "noise": SID.WAVE_NOISE,
}

# Standard MIDI note names. Aligned so note 60 = C-4.
_NOTE_NAMES = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")

# Mod-wheel (CC1) → pulse-width window. A pulse wave collapses to silent DC
# at both 0% and 100% duty, so the wheel is mapped into an audible window
# rather than the raw 0..4095 register range — wheel-to-zero used to mute
# the voices. Mid-wheel lands near 50% (a square wave).
_PW_MIN_AUDIBLE = 128  # ~3% duty
_PW_MAX_AUDIBLE = 3968  # ~97% duty

# Max rate at which coalesced continuous controllers (pitch bend, mod wheel,
# filter cutoff, volume) are flushed to the SID. 60 Hz is smooth to the ear
# and keeps wheel sweeps from bursting the DMA socket. See _reader().
_CONTROL_FLUSH_INTERVAL_S = 1.0 / 60.0

# SHIFT cycles the global waveform through these, in order.
_WAVEFORM_CYCLE = ("pulse", "sawtooth", "triangle", "noise")

# Control-change (CC) numbers → SID parameters. General-MIDI-ish where it maps
# cleanly (CC73/72/75 = attack/release/decay sound controllers; CC71 = harmonic
# content → resonance; CC74 = brightness → filter cutoff). See _control_change.
_CC_MODWHEEL = 1  # pulse width sweep
_CC_VOLUME = 7  # master volume
_CC_RESONANCE = 71  # filter resonance
_CC_RELEASE = 72  # envelope release
_CC_ATTACK = 73  # envelope attack
_CC_CUTOFF = 74  # filter cutoff
_CC_DECAY = 75  # envelope decay

# Idle voice strips are drawn in this gray (a released voice's flat trace reads
# as "off"); a sounding voice repaints in its configured/per-waveform color.
_IDLE_GRAY = "gray"
# Envelope level below which a voice counts as silent/idle (drives the
# colored-vs-gray strip). Matches WaveformScene's silence epsilon.
_ENV_SILENCE_EPS = 1e-3


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

    Voice allocation layers a monophonic melody line over a polyphonic
    sustain pad: held notes keep their voice, and a new note that needs a
    voice when all three are sounding steals the most-recently-started one
    (so the older/held notes stay put). See _note_on / _note_off. Per-voice
    waveform / ADSR / pulse width come from the scene config (ADSR + filter
    are also live-tweakable via CC; see _control_change).
    """

    WANTS_AUDIO_LOCK = True

    def __init__(
        self,
        api,
        audio,
        port: str | None = None,
        waveform: str = "pulse",
        adsr: tuple[int, int, int, int] = (0, 8, 12, 8),
        pulse_width: int = 2048,
        filter_cutoff: int = 2047,
        filter_resonance: int = 0,
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
        name: str = "MIDI",
    ):
        super().__init__(api, audio, None, name)
        if not MIDI_AVAILABLE:
            raise RuntimeError(
                "MidiScene requires mido + python-rtmidi (pip install c64cast[midi])"
            )
        if waveform not in _WAVEFORM_BITS:
            raise ValueError(
                f"MidiScene: waveform must be one of {sorted(_WAVEFORM_BITS)}, got {waveform!r}"
            )
        if len(adsr) != 4 or not all(0 <= v <= 15 for v in adsr):
            raise ValueError(f"MidiScene: adsr must be 4 ints in 0..15, got {adsr!r}")
        if not 0 <= pulse_width <= 4095:
            raise ValueError("MidiScene: pulse_width must be 0..4095")
        if not 0 <= filter_cutoff <= 2047:
            raise ValueError("MidiScene: filter_cutoff must be 0..2047 (11-bit)")
        if not 0 <= filter_resonance <= 15:
            raise ValueError("MidiScene: filter_resonance must be 0..15")
        if not 0 <= master_volume <= 15:
            raise ValueError("MidiScene: master_volume must be 0..15")

        self.port_name = port
        self.waveform = waveform
        self.waveform_bits = _WAVEFORM_BITS[waveform]
        # Mutable so the ADSR CCs (CC73/72/75) can update attack/decay/release
        # live. Sustain (index 2) is the base/fallback; per-note velocity
        # overrides it in _program_voice (velocity → loudness).
        self.adsr = list(adsr)
        self.pulse_width = int(pulse_width)
        self.filter_cutoff = int(filter_cutoff)
        self.filter_resonance = int(filter_resonance)
        self.filter_mode = filter_mode
        self.master_volume = int(master_volume)
        self.system = system

        # Voice trace colors. Pad the configured names to 3 with C64-friendly
        # defaults; the scope mixin (via _init_scope_knobs) resolves these to
        # palette indices and requires at least 3.
        default_colors = ["light green", "cyan", "yellow"]
        names = list(voice_colors) if voice_colors else list(default_colors)
        if len(names) < SID.N_VOICES:
            names = (names + default_colors[len(names) :])[: SID.N_VOICES]

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
        self._sid_shadow = bytearray(SID_REG_COUNT)  # mirrors $D400-$D418
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
        # Per-voice display state, change-detected in process_frame so the
        # strip color is repainted only on a transition: _voice_sounding =
        # gated-or-decaying (drives colored-vs-gray); _last_voice_wave = the
        # current waveform (drives per_waveform recoloring).
        self._voice_sounding: list[bool] = [False, False, False]
        self._last_voice_wave: list[int] = [-1, -1, -1]

        self.voices: list[_VoiceState] = [_VoiceState() for _ in range(SID.N_VOICES)]
        # Stack of all currently-held MIDI notes (most-recent last) + their
        # press velocities; the top SID.N_VOICES sound. See _reconcile_voices.
        self._held: list[int] = []
        self._held_vel: dict[int, int] = {}
        self._allocation_lock = threading.Lock()
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
                raise RuntimeError("MidiScene: no MIDI input ports available")
            self._midi_port = mido.open_input(names[0])
            log.info("MidiScene: opened MIDI port %r", names[0])
            return
        # Allow partial-name matching so users don't need to paste the
        # exact rtmidi string.
        names = mido.get_input_names()
        match = next((n for n in names if self.port_name.lower() in n.lower()), None)
        if match is None:
            raise RuntimeError(
                f"MidiScene: no MIDI input port matches {self.port_name!r}; available: {names}"
            )
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
                if (
                    pending_pitch is not None or pending_cc
                ) and now - last_flush >= _CONTROL_FLUSH_INTERVAL_S:
                    for ctrl, val in pending_cc.items():
                        self._control_change(ctrl, val)
                    pending_cc = {}
                    if pending_pitch is not None:
                        self._pitchwheel(pending_pitch)
                        pending_pitch = None
                    last_flush = now
                time.sleep(0.001)  # 1 ms poll — keeps note latency tight
        except Exception:
            log.exception("MidiScene reader crashed")

    def _handle_msg(self, msg) -> None:
        if msg.type == "note_on" and msg.velocity > 0:
            self._note_on(msg.note, msg.velocity)
        elif msg.type in ("note_off",) or (msg.type == "note_on" and msg.velocity == 0):
            self._note_off(msg.note)
        elif msg.type == "control_change":
            self._control_change(msg.control, msg.value)
        elif msg.type == "pitchwheel":
            self._pitchwheel(msg.pitch)

    # ---- voice allocation: mono-melody priority over a sustain pad ----------
    # Held notes keep their voice (a stable polyphonic pad); when all three
    # voices are sounding and a new note arrives, it steals the **most-recently
    # -started** voice (`max` t_changed) rather than the oldest. So the first
    # notes you hold form a sticky pad and a later overlapping line (melody /
    # arp) cycles on the top voice, stealing itself instead of the pad. When a
    # voice frees, the most-recent still-held *suspended* note resurfaces into
    # it (LIFO) so nothing held stays silent. `self._held` is the stack of all
    # currently-held notes (most-recent last) + `_held_vel` their velocities.
    def _voice_for_note(self, midi_note: int) -> int | None:
        for i, v in enumerate(self.voices):
            if v.on and v.note == midi_note:
                return i
        return None

    def _free_voice(self) -> int | None:
        return next((i for i, v in enumerate(self.voices) if not v.on), None)

    def _note_on(self, midi_note: int, velocity: int) -> None:
        with self._allocation_lock:
            if midi_note in self._held:
                self._held.remove(midi_note)
            self._held.append(midi_note)
            self._held_vel[midi_note] = velocity
            idx = self._voice_for_note(midi_note)  # re-press → re-trigger
            if idx is None:
                idx = self._free_voice()
            if idx is None:
                # All voices sounding: steal the most-recently-started one so
                # the older/held pad voices survive.
                idx = max(range(SID.N_VOICES), key=lambda i: self.voices[i].t_changed)
            self._assign_voice(idx, midi_note, velocity)
        self._dirty = True

    def _note_off(self, midi_note: int) -> None:
        with self._allocation_lock:
            if midi_note not in self._held:
                return
            self._held.remove(midi_note)
            self._held_vel.pop(midi_note, None)
            idx = self._voice_for_note(midi_note)
            if idx is None:
                self._dirty = True  # a suspended (silent) note was lifted
                return
            # Gate the voice off, then resurrect the most-recent still-held
            # note that isn't already sounding (a suspended pad/melody note).
            v = self.voices[idx]
            v.on = False
            self._program_voice(idx, midi_note, gate=False)
            sounding = {vv.note for vv in self.voices if vv.on}
            resurrect = next((n for n in reversed(self._held) if n not in sounding), None)
            if resurrect is not None:
                self._assign_voice(idx, resurrect, self._held_vel.get(resurrect, 100))
            self._dirty = True

    def _assign_voice(self, idx: int, note: int, velocity: int) -> None:
        v = self.voices[idx]
        v.note = note
        v.on = True
        v.t_changed = time.time()
        v.velocity = velocity
        self._program_voice(idx, note, gate=True, velocity=velocity)

    def _control_change(self, cc: int, value: int) -> None:
        """Map a MIDI continuous controller to a SID parameter. `value` is
        0..127. Unmapped CCs are ignored. The bottom controller row shows the
        live state (see _build_controller_line)."""
        if cc == _CC_VOLUME:  # master volume nibble
            self.master_volume = value >> 3
            self._write_mode_vol()
        elif cc == _CC_MODWHEEL:  # pulse width sweep
            # Map the wheel into an audible window so wheel-to-zero (or full)
            # doesn't drive the pulse to a silent 0% / 100% duty.
            span = _PW_MAX_AUDIBLE - _PW_MIN_AUDIBLE
            self.pulse_width = _PW_MIN_AUDIBLE + (value * span) // 127
            self._write_pulse_width()
        elif cc == _CC_CUTOFF:  # filter cutoff (11-bit)
            self.filter_cutoff = (value << 4) & 0x07FF
            self._write_filter()
        elif cc == _CC_RESONANCE:  # filter resonance (4-bit)
            self.filter_resonance = value >> 3
            self._write_filter()
        elif cc == _CC_ATTACK:  # envelope attack
            self.adsr[0] = value >> 3
            self._write_envelope_regs()
        elif cc == _CC_DECAY:  # envelope decay
            self.adsr[1] = value >> 3
            self._write_envelope_regs()
        elif cc == _CC_RELEASE:  # envelope release
            self.adsr[3] = value >> 3
            self._write_envelope_regs()
        else:
            return
        self._feed_emulator()  # keep the host emulator's trace in sync
        self._dirty = True  # controller row reflects the new value

    def _pitchwheel(self, pitch: int) -> None:
        # Re-emit frequency for every gated voice with a +/- 2 semitone bend.
        bend_semitones = (pitch / 8192.0) * 2.0
        touched = False
        for idx, v in enumerate(self.voices):
            if v.on and v.note is not None:
                freq = _note_to_sid_freq(v.note + bend_semitones, self.system)
                base = SID.voice_base(idx)
                self.api.write_regs(
                    f"{base + SID.OFF_FREQ_LO:04X}",
                    freq & 0xFF,
                    (freq >> 8) & 0xFF,
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
            self.emulator.update_registers(bytes(self._sid_shadow), retrigger=retrigger)

    def _voice_sustain(self, voice_idx: int) -> int:
        """Sustain nibble for a voice: velocity-derived while it's sounding
        (velocity → loudness, since the SID has no per-voice volume — only the
        ADSR sustain level), the configured sustain otherwise."""
        v = self.voices[voice_idx]
        if v.on:
            return (v.velocity >> 3) & 0xF  # 0..127 → 0..15
        return self.adsr[2] & 0xF

    def _mode_bits(self) -> int:
        # Filter mode in the high nibble of $D418: lp=1, bp=2, hp=4.
        return {"lowpass": 0x1, "bandpass": 0x2, "highpass": 0x4}.get(self.filter_mode, 0x1)

    def _res_filt_byte(self) -> int:
        # $D417: high nibble = resonance, low 3 bits route voices 1-3 to the
        # filter. We route all three so the cutoff/resonance CCs are audible
        # (with no routing the filter does nothing — the old default).
        return ((self.filter_resonance & 0xF) << 4) | 0x07

    def _write_mode_vol(self) -> None:
        mv = ((self._mode_bits() & 0xF) << 4) | (self.master_volume & 0xF)
        self.api.write_memory(f"{SID.MODE_VOL:04X}", f"{mv:02X}")
        self._poke_shadow(SID.MODE_VOL, mv)

    def _write_filter(self) -> None:
        fc = self.filter_cutoff
        self.api.write_regs(f"{SID.FC_LO:04X}", fc & 0x07, (fc >> 3) & 0xFF)
        self._poke_shadow(SID.FC_LO, fc & 0x07)
        self._poke_shadow(SID.FC_HI, (fc >> 3) & 0xFF)
        rf = self._res_filt_byte()
        self.api.write_memory(f"{SID.RES_FILT:04X}", f"{rf:02X}")
        self._poke_shadow(SID.RES_FILT, rf)

    def _write_pulse_width(self) -> None:
        for vidx in range(SID.N_VOICES):
            base = SID.voice_base(vidx)
            self.api.write_regs(
                f"{base + SID.OFF_PW_LO:04X}",
                self.pulse_width & 0xFF,
                (self.pulse_width >> 8) & 0x0F,
            )
            self._poke_shadow(base + SID.OFF_PW_LO, self.pulse_width & 0xFF)
            self._poke_shadow(base + SID.OFF_PW_HI, (self.pulse_width >> 8) & 0x0F)

    def _write_envelope_regs(self) -> None:
        """Push AD (attack/decay) + SR (per-voice sustain / release) to all
        three voices — used when an ADSR CC changes the envelope."""
        a, d = self.adsr[0], self.adsr[1]
        r = self.adsr[3]
        ad = ((a & 0xF) << 4) | (d & 0xF)
        for vidx in range(SID.N_VOICES):
            base = SID.voice_base(vidx)
            sr = ((self._voice_sustain(vidx) & 0xF) << 4) | (r & 0xF)
            self.api.write_regs(f"{base + SID.OFF_AD:04X}", ad, sr)
            self._poke_shadow(base + SID.OFF_AD, ad)
            self._poke_shadow(base + SID.OFF_SR, sr)

    def _program_voice(
        self, voice_idx: int, midi_note: int, gate: bool, velocity: int | None = None
    ) -> None:
        base = SID.voice_base(voice_idx)
        freq = _note_to_sid_freq(midi_note, self.system)
        # Coalesce freq + pulse-width + control + ADSR (7 contiguous bytes)
        # into a single PUT — minimises socket overhead per note event.
        ctrl = self.waveform_bits | (SID.GATE if gate else 0)
        a, d, _, r = self.adsr
        # Velocity → sustain (loudness) on a gated note; configured sustain
        # otherwise (note_off keeps the last SR; the gate clear is what matters).
        sustain = (velocity >> 3) & 0xF if (gate and velocity is not None) else self.adsr[2] & 0xF
        regs = (
            freq & 0xFF,
            (freq >> 8) & 0xFF,
            self.pulse_width & 0xFF,
            (self.pulse_width >> 8) & 0x0F,
            ctrl,
            ((a & 0xF) << 4) | (d & 0xF),
            ((sustain & 0xF) << 4) | (r & 0xF),
        )
        off = voice_idx * SID.BYTES_PER_VOICE
        prev_ctrl = self._sid_shadow[off + SID.OFF_CONTROL]
        # Hard re-trigger: the real SID's envelope only attacks on a gate 0→1
        # edge. Re-gating a voice that's already gated (re-press, voice steal,
        # a trill cycling one voice) writes gate=1 with no edge, so the chip
        # changes pitch but never re-attacks — the note is silent (only the
        # host emulator, fed the retrigger flag below, re-attacks → "waveform
        # moves but no sound"). Force the edge by clearing the gate first.
        hard_restart = gate and bool(prev_ctrl & SID.GATE)
        if hard_restart:
            self.api.write_memory(
                f"{base + SID.OFF_CONTROL:04X}", f"{self.waveform_bits & 0xFF:02X}"
            )  # gate off
        self.api.write_regs(f"{base:04X}", *regs)

        # Mirror into the shadow + feed the emulator. The same hard-restart
        # case is invisible to update_registers' gate-edge detection (the
        # shadow's previous control byte was gated), so flag it explicitly.
        self._sid_shadow[off : off + SID.BYTES_PER_VOICE] = bytes(regs)
        retrigger: tuple[bool, ...] | None = None
        if hard_restart:
            mask = [False] * SID.N_VOICES
            mask[voice_idx] = True
            retrigger = tuple(mask)
        self._feed_emulator(retrigger=retrigger)

    def _program_global_sid(self) -> None:
        # Route all three voices through the filter (so the cutoff/resonance
        # CCs are audible) + set the filter mode and master volume. Cutoff
        # defaults open, so a lowpass patch is neutral until CC74 sweeps it.
        self._write_filter()
        self._write_mode_vol()

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
                self.waveform_bits,  # gate off
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
        return _layout_lr(f"MIDI {self.waveform.upper()}", f"VOL {self.master_volume:2d}")

    def _build_controller_line(self) -> str:
        """Live controller state on the second row: pulse width %, filter
        cutoff (OPEN at max), resonance, and the A/D/R envelope nibbles. Per-
        voice note/velocity isn't shown — the colored-vs-gray voice strips
        already convey which voices are sounding."""
        pw_pct = round(self.pulse_width * 100 / 4095)
        cut = "OPEN" if self.filter_cutoff >= 0x07FF else f"{self.filter_cutoff:4d}"
        a, d, _, r = self.adsr
        line = f"PW{pw_pct:3d}% CUT {cut} RES {self.filter_resonance:2d} A{a:X} D{d:X} R{r:X}"
        return line[:40].ljust(40)

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
            self._build_controller_line(),
            meta_fg,
            RegionID.WAVE_META_BITMAP,
            RegionID.WAVE_META_SCREEN,
        )

    # ---- SHIFT: cycle the global waveform ------------------------------------
    def cycle_style(self, api) -> str:
        """SHIFT handler: advance the global waveform pulse→saw→tri→noise and
        re-emit it on every voice (held notes keep their gate + velocity
        sustain; idle voices get the new waveform for their next note)."""
        i = _WAVEFORM_CYCLE.index(self.waveform) if self.waveform in _WAVEFORM_CYCLE else -1
        self.waveform = _WAVEFORM_CYCLE[(i + 1) % len(_WAVEFORM_CYCLE)]
        self.waveform_bits = _WAVEFORM_BITS[self.waveform]
        with self._allocation_lock:
            for idx, v in enumerate(self.voices):
                if v.on and v.note is not None:
                    self._program_voice(idx, v.note, gate=True, velocity=v.velocity)
                else:
                    base = SID.voice_base(idx)
                    self.api.write_memory(
                        f"{base + SID.OFF_CONTROL:04X}", f"{self.waveform_bits:02X}"
                    )
                    self._sid_shadow[idx * SID.BYTES_PER_VOICE + SID.OFF_CONTROL] = (
                        self.waveform_bits
                    )
        self._feed_emulator()
        self._dirty = True
        return f"waveform={self.waveform}"

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
        # Start every voice strip gray (idle): _apply_vic_hires_bank painted
        # them in their sounding colors, but nothing is playing yet. They flip
        # to color on note-on (see process_frame).
        self._voice_sounding = [False, False, False]
        self._last_voice_wave = [-1, -1, -1]
        for idx in range(SID.N_VOICES):
            self._repaint_voice_color(idx, C64_COLORS[_IDLE_GRAY])
        self._paint_info_rows()
        self._alloc_scope_buffers()
        self._open_port()
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._reader, daemon=True, name="midi-reader")
        self._reader_thread.start()
        # Envelope ticker: advances each voice's ADSR at the video rate so
        # attack/decay/release tails evolve on screen between MIDI events.
        self._poll = PollThread(self._tick_envelopes, period=self._poll_dt, name="midi-env")
        self._poll.start()
        self._dirty = True

    def _tick_envelopes(self) -> None:
        with self._reg_lock:
            self.emulator.advance_envelopes(self._poll_dt)

    def process_frame(self, current_time: float) -> bool:
        # Activity coloring: a sounding voice (gated, or still decaying) draws
        # in its color; an idle voice fades to gray. Change-detected so the
        # screen-RAM color write only fires on a transition (sounding flip, or
        # — in per_waveform mode — a waveform change while sounding).
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
            self.api.write_memory(f"{CIA2.PORT_A:04X}", f"{CIA2.PORT_A_BANK_0:02X}")
            self.api.write_memory("d018", f"{D018_HIRES_BITMAP:02X}")
            self.api.flush()
        except Exception:
            log.debug("MidiScene: teardown silence/restore failed", exc_info=True)
