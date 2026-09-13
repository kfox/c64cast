"""MIDI → SID scene. Drives the C64's SID directly from incoming MIDI, and
visualizes the three voices as a full-screen hires oscilloscope.

A live MIDI input port (keyboard, DAW, looper) becomes a real-time control
surface for the SID's three voices: note on/off map to voice frequency + gate,
pitch-bend and a few CC numbers to common SID parameters. The SID's native
voices generate the sound — the audio NMI loop is not used, and the only
register the two contend over is the $D418 volume nibble (last writer wins).

Visualization is the shared
:class:`~c64cast.sid.voice_scope.VoiceScopeRenderer` oscilloscope. Unlike
WaveformScene, MidiScene *is* the writer — it computes every SID byte it sends —
so it keeps a 25-byte $D400-$D418 register shadow and feeds the host-side
:class:`~c64cast.sid.sidemu.SIDEmulator` directly, with no parallel py65 6502.

Display is bitmap-only, so PETSCII overlays don't apply.

Requires the `midi` extra (``uv tool install --force 'c64cast[all]'``).

See docs/architecture/sid.md#midi_scenepy--midiscene-live-midi--sid--oscilloscope.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from functools import partial

from c64cast._midi import MAX_DRAIN_WORK_S, MIDI_AVAILABLE, open_input_port, poll_pending
from c64cast._pollthread import PollThread
from c64cast._teardown import run_teardown_steps
from c64cast.hw.backend import HardwareProfile
from c64cast.hw.c64 import CIA2, SID, VIC_BANK_0, cpu_clock
from c64cast.scenes.scenes import Scene
from c64cast.video.palette import C64_COLORS

from .sidemu import SID_REG_COUNT, SIDEmulator, primary_waveform
from .voice_scope import (
    D018_HIRES_BITMAP,
    VoiceScopeRenderer,
    _layout_lr,
    restore_char_mode_display,
)

log = logging.getLogger(__name__)

# SID waveform-select control bits (high nibble of voice control register).
_WAVEFORM_BITS = {
    "triangle": SID.WAVE_TRIANGLE,
    "sawtooth": SID.WAVE_SAWTOOTH,
    "pulse": SID.WAVE_PULSE,
    "noise": SID.WAVE_NOISE,
}

# Canonical token order, so a parsed spec round-trips to one string regardless
# of input order ("triangle+pulse" → "pulse+triangle") and matches
# _WAVEFORM_CYCLE entries.
_WAVEFORM_CANONICAL_ORDER = ("pulse", "sawtooth", "triangle", "noise")
_WAVEFORM_ABBREV = {"triangle": "TRI", "sawtooth": "SAW", "pulse": "PUL", "noise": "NOI"}
# Inverse of _WAVEFORM_BITS: a combined voice's dominant single waveform.
_BIT_TO_NAME = {v: k for k, v in _WAVEFORM_BITS.items()}


def parse_waveform_spec(spec: str) -> tuple[int, str]:
    """Parse a waveform spec into ``(control-byte waveform bits, canonical name)``.

    Accepts a single waveform (``"pulse"``) or a ``+``-joined combination
    (``"pulse+triangle"``) of triangle/sawtooth/pulse/noise. A combination ORs
    the selected waveform-select bits — the real SID then ANDs those waveforms
    together (the oscilloscope draws the dominant one via ``primary_waveform``).
    The returned name is normalized to :data:`_WAVEFORM_CANONICAL_ORDER`.
    """
    tokens = [t.strip().lower() for t in str(spec).split("+") if t.strip()]
    if not tokens:
        raise ValueError(f"empty waveform spec: {spec!r}")
    bits = 0
    seen: set[str] = set()
    for t in tokens:
        if t not in _WAVEFORM_BITS:
            raise ValueError(f"unknown waveform {t!r} in {spec!r}; valid: {sorted(_WAVEFORM_BITS)}")
        bits |= _WAVEFORM_BITS[t]
        seen.add(t)
    name = "+".join(w for w in _WAVEFORM_CANONICAL_ORDER if w in seen)
    return bits, name


def _abbrev_waveform(name: str) -> str:
    """Short label for a (possibly combined) waveform name for the title row:
    single → TRI/SAW/PUL/NOI; combo → joined first letters ('pulse+triangle' →
    'P+T')."""
    tokens = [t for t in name.split("+") if t]
    if len(tokens) == 1:
        return _WAVEFORM_ABBREV.get(tokens[0], tokens[0][:3].upper())
    return "+".join(t[0].upper() for t in tokens)


# Mod-wheel (CC1) → pulse-width window. A pulse wave collapses to silent DC at
# both 0% and 100% duty, so the wheel maps into an audible window rather than
# the raw 0..4095 register range. Mid-wheel lands near 50% (a square wave).
_PW_MIN_AUDIBLE = 128  # ~3% duty
_PW_MAX_AUDIBLE = 3968  # ~97% duty

# Max flush rate for coalesced continuous controllers (pitch bend, mod wheel,
# filter cutoff, volume): smooth to the ear, and slow enough that a wheel sweep
# cannot burst the DMA socket.
_CONTROL_FLUSH_INTERVAL_S = 1.0 / 60.0

# Blocking link writes one note message can cost `_program_voice`: the gate-off
# byte that forces the envelope's 0->1 edge on a re-press, plus the seven-
# register voice block. Both are paid on the reader thread inside the drain.
_WRITES_PER_NOTE = 2

# Notes one drain pass must retire whatever they cost. A chord arrives as one
# message per voice, and splitting it across passes spreads its attacks by the
# reader's 1 ms poll sleep apiece — an audible arpeggio.
_NOTES_PER_DRAIN = SID.N_VOICES


def _drain_budget_s(profile: HardwareProfile) -> float:
    """Work budget for one `_reader` drain pass on `profile`'s link.

    `_midi.MAX_DRAIN_WORK_S` is sized for a consumer whose per-message cost is
    microseconds, which is true of `AsidScene._handle_sysex` and false here:
    `_handle_msg` reaches `_program_voice`, which blocks on the link, and on an
    Ultimate one write costs 5.222 ms against a 4.167 ms default budget.

    Never *tighter* than the shared default — a link whose writes are cheap
    (TeensyROM, 0.287 ms) keeps the default's larger pass — and widened only far
    enough that a full chord of worst-case notes still retires in one pass. On an
    Ultimate that lands at 31.332 ms, 1.88x `_CONTROL_FLUSH_INTERVAL_S`, so this
    budget deliberately breaks the "a fraction of the flush period it protects"
    relationship `MAX_DRAIN_WORK_S` keeps: a pass that spends the whole budget
    delays one wheel/CC flush by the difference. Both halves are pinned in
    tests/test_midi.py.
    """
    note_cost_s = profile.write_cost_s(SID.BYTES_PER_VOICE) * _WRITES_PER_NOTE
    return max(MAX_DRAIN_WORK_S, note_cost_s * _NOTES_PER_DRAIN)


# SHIFT advances each voice one step through these: the four single waveforms,
# then the combined waveforms that actually SOUND on a real SID. Only
# pulse+triangle qualifies — on a 6581 the waveform-select outputs share a bus
# and AND together, and any combination containing sawtooth ANDs down to
# near-silence, with the scope trace moving while the chip is mute. Other combos
# stay reachable via midi_voice_waveforms. Entries must be canonical.
_WAVEFORM_CYCLE = (
    "pulse",
    "sawtooth",
    "triangle",
    "noise",
    "pulse+triangle",
)

# Voice-allocation modes. "shared" = one pool of 3 voices fed by all channels
# (the historical mono-melody-over-pad allocator). "multitimbral" = MIDI
# channels route to fixed voices (see _note_on_mt). config._MIDI_VOICE_MODE_CHOICES
# mirrors this (asserted by tests/test_introspect.py ChoiceVocabSyncTest).
VOICE_MODES = ("shared", "multitimbral")

# MIDI Program Change number → waveform spec, indexed modulo the table length.
# Only pulse+triangle among the combos, for the reason _WAVEFORM_CYCLE gives.
_PC_WAVEFORMS = (
    "triangle",
    "sawtooth",
    "pulse",
    "noise",
    "pulse+triangle",
)

# Control-change (CC) numbers → SID parameters, General-MIDI-ish where it maps
# cleanly: CC73/72/75 are the attack/release/decay sound controllers, CC71
# harmonic content → resonance, CC74 brightness → filter cutoff.
_CC_MODWHEEL = 1  # pulse width sweep
_CC_VOLUME = 7  # master volume
_CC_RESONANCE = 71  # filter resonance
_CC_RELEASE = 72  # envelope release
_CC_ATTACK = 73  # envelope attack
_CC_CUTOFF = 74  # filter cutoff
_CC_DECAY = 75  # envelope decay

# Idle voice strips are drawn in this gray, so a released voice's flat trace
# reads as "off"; a sounding voice repaints in its own color.
_IDLE_GRAY = "gray"
# Envelope level below which a voice counts as idle. Matches WaveformScene's.
_ENV_SILENCE_EPS = 1e-3


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

    __slots__ = ("note", "on", "seq", "velocity")

    def __init__(self) -> None:
        self.note: int | None = None
        self.on: bool = False
        # A counter, not a timestamp: `max()` breaks a tie toward the lowest
        # index, which would steal the oldest pad voice instead of the newest,
        # and a clock coarse enough to give a chord's note-ons one shared value
        # cannot promise the distinct values voice stealing needs.
        self.seq: int = 0
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
        voice_waveforms: list[str] | None = None,
        voice_mode: str = "shared",
        voice_channels: list[int] | None = None,
        program_change: bool = True,
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
                "MidiScene requires mido + python-rtmidi (uv tool install --force 'c64cast[all]')"
            )
        if waveform not in _WAVEFORM_BITS:
            raise ValueError(
                f"MidiScene: waveform must be one of {sorted(_WAVEFORM_BITS)}, got {waveform!r}"
            )
        if voice_mode not in VOICE_MODES:
            raise ValueError(
                f"MidiScene: voice_mode must be one of {VOICE_MODES}, got {voice_mode!r}"
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
        # Global "shared default" waveform — the starting point for new/idle
        # voices in shared mode, and what the title label/back-compat track.
        self.waveform = waveform
        self.waveform_bits = _WAVEFORM_BITS[waveform]
        # Authoritative for what each voice plays. An empty config gives every
        # voice the global default; a provided list is parsed (combos allowed)
        # and padded to 3 voices by repeating the last entry.
        if voice_waveforms:
            parsed = [parse_waveform_spec(w) for w in voice_waveforms]
            parsed = (parsed + [parsed[-1]] * SID.N_VOICES)[: SID.N_VOICES]
        else:
            parsed = [(self.waveform_bits, self.waveform)] * SID.N_VOICES
        self.voice_wave_bits: list[int] = [b for b, _ in parsed]
        self.voice_wave_names: list[str] = [n for _, n in parsed]

        self.voice_mode = voice_mode
        self.program_change_enabled = bool(program_change)
        # Multitimbral channel→voice map: config channels are 1-based (1..16),
        # mido message channels are 0-based, so store 0-based keys. Default maps
        # channels 1/2/3 → voices 0/1/2. Only consulted in multitimbral mode.
        chans = list(voice_channels) if voice_channels else [1, 2, 3]
        self._chan_to_voice: dict[int, int] = {
            (ch - 1): vi for vi, ch in enumerate(chans[: SID.N_VOICES])
        }
        # Mutable so the ADSR CCs can update attack/decay/release live. Sustain
        # (index 2) is the fallback; per-note velocity overrides it.
        self.adsr = list(adsr)
        self.pulse_width = int(pulse_width)
        self.filter_cutoff = int(filter_cutoff)
        self.filter_resonance = int(filter_resonance)
        self.filter_mode = filter_mode
        self.master_volume = int(master_volume)
        self.system = system

        # The scope mixin resolves these to palette indices and requires 3.
        default_colors = ["light green", "cyan", "yellow"]
        names = list(voice_colors) if voice_colors else list(default_colors)
        if len(names) < SID.N_VOICES:
            names = (names + default_colors[len(names) :])[: SID.N_VOICES]

        # Fixed VIC bank 0: MidiScene uploads no SID payload and leaves the
        # audio ring idle, so bank 0's display regions are always free.
        self._screen_base = VIC_BANK_0.SCREEN
        self._bitmap_base = VIC_BANK_0.BITMAP
        self._dd00 = CIA2.PORT_A_BANK_0
        self._d018 = D018_HIRES_BITMAP

        # Fed from the register shadow below rather than a py65 host emulator.
        self.emulator = SIDEmulator(system=system)
        self._reg_lock = threading.Lock()
        self._sid_shadow = bytearray(SID_REG_COUNT)  # mirrors $D400-$D418
        self._video_hz = 50.0 if system.upper() == "PAL" else 60.0
        self._poll_dt = 1.0 / self._video_hz
        self._poll: PollThread | None = None

        # Half the system video rate (30 NTSC / 25 PAL), matching WaveformScene:
        # a scope reads fine at half-rate and it halves the bitmap DMA volume.
        # The envelope poll rate is independent and stays at the full rate.
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
        # Change-detected in process_frame so a strip's color is repainted only
        # on a transition: gated-or-decaying, and the current waveform.
        self._voice_sounding: list[bool] = [False, False, False]
        self._last_voice_wave: list[int] = [-1, -1, -1]

        self.voices: list[_VoiceState] = [_VoiceState() for _ in range(SID.N_VOICES)]
        # Shared mode: stack of all currently-held MIDI notes (most-recent last)
        # + their press velocities; the top SID.N_VOICES sound.
        self._held: list[int] = []
        self._held_vel: dict[int, int] = {}
        # Multitimbral mode: a per-voice held-note stack, so each routed channel
        # is monophonic with last-note priority + LIFO resurrection scoped to its
        # own voice. Unused in shared mode.
        self._mt_held: list[list[int]] = [[] for _ in range(SID.N_VOICES)]
        self._mt_held_vel: list[dict[int, int]] = [{} for _ in range(SID.N_VOICES)]
        self._allocation_lock = threading.Lock()
        # Monotonic assignment counter feeding `_VoiceState.seq`.
        self._assign_seq = 0
        self._midi_port = None
        self._reader_poll = PollThread(
            self._reader, name="midi-reader", manual=True, join_timeout=1.0
        )
        self._dirty = True  # force first text-row paint

    def _open_port(self):
        self._midi_port, _ = open_input_port(self.port_name, label="MidiScene")

    def _reader(self, stop: threading.Event):
        port = self._midi_port
        if port is None:
            return
        # A moving wheel streams hundreds of messages a second, each pitch-bend
        # fanning out to one SID write per gated voice — enough to burst the DMA
        # socket faster than the U64 accepts it, which closes the connection. So
        # each continuous controller is coalesced to its newest value and flushed
        # at a bounded rate, while notes stay discrete and immediate.
        #
        # The drain is bounded by `poll_pending`, not mido's `iter_pending`,
        # which ends only when the port queue is momentarily empty: a controller
        # outrunning `_handle_msg`'s DMA writes would never reach the flush below
        # or the `stop` check teardown's bounded join needs.
        budget_s = _drain_budget_s(self.api.profile)
        pending_pitch: int | None = None
        pending_cc: dict[int, int] = {}
        last_flush = 0.0
        try:
            while not stop.is_set():
                for msg in poll_pending(port, stop, budget_s=budget_s):
                    if msg.type == "pitchwheel":
                        pending_pitch = msg.pitch
                    elif msg.type == "control_change":
                        pending_cc[msg.control] = msg.value
                    else:
                        self._handle_msg(msg)
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
        channel = getattr(msg, "channel", 0)
        multitimbral = self.voice_mode == "multitimbral"
        if msg.type == "note_on" and msg.velocity > 0:
            if multitimbral:
                self._note_on_mt(channel, msg.note, msg.velocity)
            else:
                self._note_on(msg.note, msg.velocity)
        elif msg.type in ("note_off",) or (msg.type == "note_on" and msg.velocity == 0):
            if multitimbral:
                self._note_off_mt(channel, msg.note)
            else:
                self._note_off(msg.note)
        elif msg.type == "control_change":
            self._control_change(msg.control, msg.value)
        elif msg.type == "program_change":
            if self.program_change_enabled:
                self._program_change(msg.program, channel)
        elif msg.type == "pitchwheel":
            self._pitchwheel(msg.pitch)

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
                # Steal the most-recently-started voice, so the pad survives.
                idx = max(range(SID.N_VOICES), key=lambda i: self.voices[i].seq)
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
            # Gate off, then resurrect the most-recent still-held note that
            # isn't already sounding.
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
        # Every caller holds `_allocation_lock`, so the bump needs no lock.
        self._assign_seq += 1
        v.seq = self._assign_seq
        v.velocity = velocity
        self._program_voice(idx, note, gate=True, velocity=velocity)

    def _note_on_mt(self, channel: int, midi_note: int, velocity: int) -> None:
        idx = self._chan_to_voice.get(channel)
        if idx is None:
            return
        with self._allocation_lock:
            held = self._mt_held[idx]
            if midi_note in held:
                held.remove(midi_note)
            held.append(midi_note)
            self._mt_held_vel[idx][midi_note] = velocity
            self._assign_voice(idx, midi_note, velocity)
        self._dirty = True

    def _note_off_mt(self, channel: int, midi_note: int) -> None:
        idx = self._chan_to_voice.get(channel)
        if idx is None:
            return
        with self._allocation_lock:
            held = self._mt_held[idx]
            if midi_note not in held:
                return
            held.remove(midi_note)
            self._mt_held_vel[idx].pop(midi_note, None)
            v = self.voices[idx]
            # Only react if the released note is the one currently sounding;
            # releasing a non-top held note just leaves the stack.
            if not (v.on and v.note == midi_note):
                self._dirty = True
                return
            v.on = False
            self._program_voice(idx, midi_note, gate=False)
            if held:  # resurrect this voice's most-recent still-held note
                resurrect = held[-1]
                self._assign_voice(idx, resurrect, self._mt_held_vel[idx].get(resurrect, 100))
            self._dirty = True

    def _control_change(self, cc: int, value: int) -> None:
        """Map a MIDI continuous controller to a SID parameter. `value` is
        0..127. Unmapped CCs are ignored. The bottom controller row shows the
        live state (see _build_meta_line)."""
        if cc == _CC_VOLUME:
            self.master_volume = value >> 3
            self._write_mode_vol()
        elif cc == _CC_MODWHEEL:
            span = _PW_MAX_AUDIBLE - _PW_MIN_AUDIBLE
            self.pulse_width = _PW_MIN_AUDIBLE + (value * span) // 127
            self._write_pulse_width()
        elif cc == _CC_CUTOFF:  # filter cutoff (11-bit)
            self.filter_cutoff = (value << 4) & 0x07FF
            self._write_filter()
        elif cc == _CC_RESONANCE:  # filter resonance (4-bit)
            self.filter_resonance = value >> 3
            self._write_filter()
        elif cc == _CC_ATTACK:
            self.adsr[0] = value >> 3
            self._write_envelope_regs()
        elif cc == _CC_DECAY:
            self.adsr[1] = value >> 3
            self._write_envelope_regs()
        elif cc == _CC_RELEASE:
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
        # filter. All three are routed, or the cutoff/resonance CCs sweep a
        # filter no voice passes through.
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
        # freq + pulse-width + control + ADSR are 7 contiguous bytes: one PUT.
        wave_bits = self.voice_wave_bits[voice_idx]
        ctrl = wave_bits | (SID.GATE if gate else 0)
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
        # The real SID's envelope only attacks on a gate 0→1 edge. Re-gating an
        # already-gated voice (re-press, voice steal, a trill) writes gate=1 with
        # no edge, so the chip changes pitch but never re-attacks and the note is
        # silent. Clearing the gate first forces the edge.
        hard_restart = gate and bool(prev_ctrl & SID.GATE)
        if hard_restart:
            self.api.write_memory(
                f"{base + SID.OFF_CONTROL:04X}", f"{wave_bits & 0xFF:02X}"
            )  # gate off
        self.api.write_regs(f"{base:04X}", *regs)

        # The hard restart is invisible to update_registers' gate-edge detection
        # (the shadow's previous control byte was gated), so flag it explicitly.
        self._sid_shadow[off : off + SID.BYTES_PER_VOICE] = bytes(regs)
        retrigger: tuple[bool, ...] | None = None
        if hard_restart:
            mask = [False] * SID.N_VOICES
            mask[voice_idx] = True
            retrigger = tuple(mask)
        self._feed_emulator(retrigger=retrigger)

    def _program_global_sid(self) -> None:
        # Cutoff defaults open, so a lowpass patch is neutral until CC74 sweeps.
        self._write_filter()
        self._write_mode_vol()

    def _preprogram_voices(self) -> None:
        """Seed each voice's pulse width + waveform + ADSR (gate off) on the
        SID and in the shadow so a note_on only changes freq + gate and the
        emulator starts from the configured (silent) state."""
        a, d, s, r = self.adsr
        for vidx in range(SID.N_VOICES):
            base = SID.voice_base(vidx)
            wave_bits = self.voice_wave_bits[vidx]
            self.api.write_regs(
                f"{base + SID.OFF_PW_LO:04X}",
                self.pulse_width & 0xFF,
                (self.pulse_width >> 8) & 0x0F,
                wave_bits,  # gate off
                ((a & 0xF) << 4) | (d & 0xF),
                ((s & 0xF) << 4) | (r & 0xF),
            )
            off = vidx * SID.BYTES_PER_VOICE
            self._poke_shadow(base + SID.OFF_PW_LO, self.pulse_width & 0xFF)
            self._poke_shadow(base + SID.OFF_PW_HI, (self.pulse_width >> 8) & 0x0F)
            self._sid_shadow[off + SID.OFF_CONTROL] = wave_bits
            self._sid_shadow[off + SID.OFF_AD] = ((a & 0xF) << 4) | (d & 0xF)
            self._sid_shadow[off + SID.OFF_SR] = ((s & 0xF) << 4) | (r & 0xF)
        self._feed_emulator()

    def _build_title_line(self) -> str:
        """Per-voice waveforms (left), master volume (right). Each voice shows a
        short waveform tag (TRI/SAW/PUL/NOI; a combo joins first letters, e.g.
        'P+T') so all three timbres are visible at a glance."""
        waves = " ".join(
            f"{i + 1}:{_abbrev_waveform(n)}" for i, n in enumerate(self.voice_wave_names)
        )
        return _layout_lr(waves, f"VOL {self.master_volume:2d}")

    def _build_meta_line(self) -> str:
        """Live controller state on the second row: pulse width %, filter
        cutoff (OPEN at max), resonance, and the A/D/R envelope nibbles. Per-
        voice note/velocity isn't shown — the colored-vs-gray voice strips
        already convey which voices are sounding."""
        pw_pct = round(self.pulse_width * 100 / 4095)
        cut = "OPEN" if self.filter_cutoff >= 0x07FF else f"{self.filter_cutoff:4d}"
        a, d, _, r = self.adsr
        line = f"PW{pw_pct:3d}% CUT {cut} RES {self.filter_resonance:2d} A{a:X} D{d:X} R{r:X}"
        return line[:40].ljust(40)

    def _set_voice_waveform(self, idx: int, bits: int, name: str) -> None:
        """Set voice `idx`'s waveform and re-emit it: a sounding voice is
        re-programmed (hard restart keeps the gate + velocity sustain), an idle
        voice just gets its control byte updated so its next note uses the new
        waveform. Caller holds `_allocation_lock`."""
        self.voice_wave_bits[idx] = bits
        self.voice_wave_names[idx] = name
        v = self.voices[idx]
        if v.on and v.note is not None:
            self._program_voice(idx, v.note, gate=True, velocity=v.velocity)
        else:
            base = SID.voice_base(idx)
            self.api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{bits & 0xFF:02X}")
            self._sid_shadow[idx * SID.BYTES_PER_VOICE + SID.OFF_CONTROL] = bits & 0xFF
            self._feed_emulator()

    def _waveform_label(self) -> str:
        names = self.voice_wave_names
        if all(n == names[0] for n in names):
            return f"waveform={names[0]}"
        return "waveforms=" + "/".join(_abbrev_waveform(n) for n in names)

    def _program_change(self, program: int, channel: int) -> None:
        """MIDI Program Change → waveform. Shared mode sets the waveform on all
        three voices (a global patch change); multitimbral sets only the voice
        mapped to the message's channel."""
        bits, name = parse_waveform_spec(_PC_WAVEFORMS[program % len(_PC_WAVEFORMS)])
        with self._allocation_lock:
            if self.voice_mode == "multitimbral":
                idx = self._chan_to_voice.get(channel)
                if idx is None:
                    return
                targets = [idx]
            else:
                targets = list(range(SID.N_VOICES))
                self.waveform = name
                self.waveform_bits = bits
            for idx in targets:
                self._set_voice_waveform(idx, bits, name)
        self._dirty = True

    def cycle_style(self, api) -> str:
        """SHIFT handler: advance every voice one step through `_WAVEFORM_CYCLE`
        (the four single waveforms, then combined waveforms), keeping per-voice
        offsets. A uniform set cycles in lockstep (the historical behavior);
        differing per-voice waveforms each walk from their own position so the
        voices stay distinct and each tours the combos. Held voices keep their
        gate + velocity sustain; idle voices get the new waveform for their
        next note."""
        with self._allocation_lock:
            for idx, name in enumerate(self.voice_wave_names):
                if name in _WAVEFORM_CYCLE:
                    i = _WAVEFORM_CYCLE.index(name)
                else:
                    # An off-cycle combo advances from its dominant single
                    # waveform's slot; resetting to index 0 would collapse
                    # distinct voices onto the same waveform.
                    dom = _BIT_TO_NAME[primary_waveform(self.voice_wave_bits[idx])]
                    i = _WAVEFORM_CYCLE.index(dom)
                bits, cname = parse_waveform_spec(_WAVEFORM_CYCLE[(i + 1) % len(_WAVEFORM_CYCLE)])
                self._set_voice_waveform(idx, bits, cname)
            # Voice 0 tracks the shared default / single-waveform label.
            self.waveform = self.voice_wave_names[0]
            self.waveform_bits = self.voice_wave_bits[0]
        self._dirty = True
        return self._waveform_label()

    def setup(self) -> None:
        super().setup()
        self._program_global_sid()
        self._preprogram_voices()
        # invalidate_cache first, so the delta cache gets a clean baseline: the
        # previous scene may have used $0400/$2000 for char-mode content.
        self.api.invalidate_cache()
        self._apply_vic_hires_bank()
        # _apply_vic_hires_bank painted the strips in their sounding colors, but
        # nothing is playing yet; process_frame flips them on note-on.
        self._voice_sounding = [False, False, False]
        self._last_voice_wave = [-1, -1, -1]
        for idx in range(SID.N_VOICES):
            self._repaint_voice_color(idx, C64_COLORS[_IDLE_GRAY])
        self._paint_info_rows()
        self._alloc_scope_buffers()
        self._open_port()
        self._reader_poll.start()
        # Advances each voice's ADSR at the video rate, so attack/decay/release
        # tails evolve on screen between MIDI events.
        self._poll = PollThread(self._tick_envelopes, period=self._poll_dt, name="midi-env")
        self._poll.start()
        self._dirty = True

    def _tick_envelopes(self) -> None:
        with self._reg_lock:
            self.emulator.advance_envelopes(self._poll_dt)

    def process_frame(self, current_time: float) -> bool:
        # A sounding voice (gated, or still decaying) draws in its color, an
        # idle one in gray. Change-detected, so the screen-RAM color write fires
        # only on a transition.
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
        steps += [
            ("SID silence", self.api.silence_sid),
            ("char-mode display restore", partial(restore_char_mode_display, self.api)),
            ("flush", self.api.flush),
        ]
        run_teardown_steps(log, type(self).__name__, steps)
