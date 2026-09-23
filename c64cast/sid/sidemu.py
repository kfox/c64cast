"""Minimal SID emulator for waveform *visualization*.

Not a full emulator — no filter, no master volume mixing, no per-cycle 6502
timing. The real SID chip plays the file and produces the audio; this module
mirrors the per-voice waveform shape + ADSR envelope from a periodic snapshot of
the SID's 25 registers ($D400-$D418).

Per-voice state: a 24-bit phase accumulator (advanced internally — it does not
need to match the real chip's phase), the waveform select bits + pulse width,
and an envelope state machine driven by the gate bit (control bit 0).

See docs/architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from c64cast.hw.c64 import SID, cpu_clock

# Re-exported under their historical names; c64.SID holds the definitions.
WAVE_TRIANGLE = SID.WAVE_TRIANGLE
WAVE_SAWTOOTH = SID.WAVE_SAWTOOTH
WAVE_PULSE = SID.WAVE_PULSE
WAVE_NOISE = SID.WAVE_NOISE
# All four waveform-select bits (control high nibble). Exactly one set = a
# single waveform; two or more = a combined waveform (see voice_samples).
WAVE_MASK = WAVE_TRIANGLE | WAVE_SAWTOOTH | WAVE_PULSE | WAVE_NOISE
GATE = SID.GATE

# SID ADSR rate table (in seconds to traverse full range, indexed by the
# 4-bit AD/SR nibble). Decay/Release uses 3× the attack time per the SID
# spec. From the resid reference.
ATTACK_TIMES_S = [
    0.002,
    0.008,
    0.016,
    0.024,
    0.038,
    0.056,
    0.068,
    0.080,
    0.100,
    0.250,
    0.500,
    0.800,
    1.000,
    3.000,
    5.000,
    8.000,
]
DECAY_TIMES_S = [t * 3.0 for t in ATTACK_TIMES_S]

NIBBLE_MASK = 0x0F
NIBBLE_MAX = 15
SID_REG_COUNT = 25  # $D400-$D418 (3 voices × 7 + 4 global)
ACCUMULATOR_BITS = 24
ACCUMULATOR_RANGE = 1 << ACCUMULATOR_BITS
PULSE_WIDTH_RANGE = 4096  # 12-bit pulse-width register max + 1
NOISE_SEED_STRIDE = 1337  # arbitrary, but stable across runs
NOISE_SEED_OFFSET = 7

# Envelope level at or below which a gated-off voice counts as no longer
# audible. Owned beside `Voice.is_audible`, its only reader, and imported by
# WaveformScene's end-of-tune watch so the two cannot drift apart.
ENV_SILENCE_EPS = 1e-3


def primary_waveform(control: int) -> int:
    """Return the dominant waveform bit in the control byte, or 0 for none.

    SID hardware ANDs combined waveforms together; we pick by priority
    instead since that yields a clean visual trace per voice."""
    if control & WAVE_NOISE:
        return WAVE_NOISE
    if control & WAVE_PULSE:
        return WAVE_PULSE
    if control & WAVE_SAWTOOTH:
        return WAVE_SAWTOOTH
    if control & WAVE_TRIANGLE:
        return WAVE_TRIANGLE
    return 0


@dataclass
class Voice:
    # Register-derived state. Updated by SIDEmulator.update_registers().
    freq: int = 0  # 16-bit
    pulse_width: int = 0  # 12-bit
    control: int = 0  # 8-bit
    ad: int = 0  # attack hi nibble, decay lo nibble
    sr: int = 0  # sustain hi nibble, release lo nibble

    # Emulator-internal state.
    accumulator: float = 0.0  # 24-bit phase, kept as float for ease
    envelope_level: float = 0.0
    envelope_state: str = "release"  # attack | decay | sustain | release
    noise_seed: int = 1  # per-voice LFSR-ish seed

    def gated(self) -> bool:
        return bool(self.control & GATE)

    def is_silent(self) -> bool:
        """True when this voice's oscillator can't produce a trace: no
        waveform selected, a zero frequency (the phase accumulator never
        advances, so every sample freezes on one phase), or a dead envelope.

        Owned here, next to the fields it reads, so `voice_samples` and
        `VoiceScopeRenderer` cannot spell it differently."""
        return primary_waveform(self.control) == 0 or self.freq == 0 or self.envelope_level <= 0.0

    def is_audible(self, eps: float = ENV_SILENCE_EPS) -> bool:
        """True while this voice is gated on, or gated off and still decaying
        above `eps` — the question the scope scenes ask to decide whether to
        draw a voice's strip in its own color or gray it out.

        A sibling of `is_silent()`, not a reuse of it: `is_silent()` asks
        whether the oscillator can produce a trace at all, this asks whether a
        released voice's tail has faded. Owned here for the same reason, next
        to the fields it reads, so MidiScene and AsidScene cannot spell it
        differently."""
        return self.gated() or self.envelope_level > eps


class SIDEmulator:
    """Stateful per-voice waveform generator.

    Usage from a scene:
        emu = SIDEmulator(system="NTSC")
        host = SidHostEmu(sid_bytes, song=1)   # sid_host_emu.py
        # each frame:
        host.tick_play()                       # advance the parallel 6502
        emu.update_registers(host.regs(), retrigger=host.retriggers())
        emu.advance_envelopes(dt_seconds)
        for v in range(3):
            samples = emu.voice_samples(v, n=320)   # float32 in [-1, 1]

    The registers have to come from a host-side source — a `SidHostEmu`
    shadow, MIDI note state, or an ASID stream. `api.read_memory(0xD400, 25)`
    cannot supply them: SID I/O is write-only, so the U64 answers with
    open-bus zeros (and the call is typed `bytes | None`, so a failed read
    hands `update_registers` a `None`). Recovering the register state without
    reading the chip is the whole reason `SidHostEmu` exists.
    """

    # Fallback visualization rate, used only when a caller of voice_samples()
    # passes no time_window_s. Every scope caller passes one, from
    # VoiceScopeRenderer._voice_time_window_s.
    SAMPLE_RATE = 22050

    def __init__(self, system: str = "NTSC"):
        # cpu_clock, not a bare `system == "NTSC"`: config.py validates
        # `[ultimate64].system` case-insensitively, so a lowercase "ntsc" would
        # take the PAL clock — 3.7% low, and it propagates into the poll rate.
        self.clock = cpu_clock(system)
        self.voices: list[Voice] = [Voice() for _ in range(SID.N_VOICES)]
        # Per-voice deterministic noise sequences so the visualization
        # is stable across reset.
        self._noise_rng = [
            random.Random(i * NOISE_SEED_STRIDE + NOISE_SEED_OFFSET) for i in range(SID.N_VOICES)
        ]

    def update_registers(self, regs: bytes, retrigger: tuple[bool, ...] | None = None):
        """Snapshot the SID's register state. Triggers gate-edge transitions
        on the envelope state machine. `regs` must be 25 bytes starting at
        $D400.

        `retrigger`, if given, is a per-voice mask of hard restarts the
        caller detected that the shadow can't show (gate pulsed off→on
        within one PLAY call — see SidHostEmu.retriggers). A flagged voice
        is forced back to attack from zero so a plucked (sustain=0) lead
        re-attacks on every note instead of flatlining after one decay."""
        if len(regs) < SID_REG_COUNT:
            return
        for v_idx in range(SID.N_VOICES):
            base = v_idx * SID.BYTES_PER_VOICE
            v = self.voices[v_idx]
            v.freq = regs[base + SID.OFF_FREQ_LO] | (regs[base + SID.OFF_FREQ_HI] << 8)
            v.pulse_width = regs[base + SID.OFF_PW_LO] | (
                (regs[base + SID.OFF_PW_HI] & NIBBLE_MASK) << 8
            )
            new_ctrl = regs[base + SID.OFF_CONTROL]
            was_gated = v.gated()
            v.control = new_ctrl
            now_gated = v.gated()
            if now_gated and not was_gated:
                v.envelope_state = "attack"
            elif was_gated and not now_gated:
                v.envelope_state = "release"
            if retrigger is not None and retrigger[v_idx]:
                # Gate pulsed off→on within the tick; the edge logic above
                # cannot see it, so re-attack from zero here.
                v.envelope_state = "attack"
                v.envelope_level = 0.0
            v.ad = regs[base + SID.OFF_AD]
            v.sr = regs[base + SID.OFF_SR]

    def advance_envelopes(self, dt_s: float):
        """Step each voice's ADSR envelope forward by `dt_s` seconds."""
        if dt_s <= 0:
            return
        for v in self.voices:
            self._advance_envelope(v, dt_s)

    @staticmethod
    def _advance_envelope(v: Voice, dt: float):
        if v.envelope_state == "attack":
            rate_s = ATTACK_TIMES_S[(v.ad >> 4) & NIBBLE_MASK]
            v.envelope_level += dt / max(rate_s, 1e-6)
            if v.envelope_level >= 1.0:
                v.envelope_level = 1.0
                v.envelope_state = "decay"
        elif v.envelope_state == "decay":
            sustain = ((v.sr >> 4) & NIBBLE_MASK) / NIBBLE_MAX
            rate_s = DECAY_TIMES_S[v.ad & NIBBLE_MASK]
            v.envelope_level -= dt / max(rate_s, 1e-6)
            if v.envelope_level <= sustain:
                v.envelope_level = sustain
                v.envelope_state = "sustain"
        elif v.envelope_state == "sustain":
            sustain = ((v.sr >> 4) & NIBBLE_MASK) / NIBBLE_MAX
            # Sustain level can change while held — track it.
            v.envelope_level = sustain
        elif v.envelope_state == "release":
            rate_s = DECAY_TIMES_S[v.sr & NIBBLE_MASK]
            v.envelope_level -= dt / max(rate_s, 1e-6)
            if v.envelope_level <= 0.0:
                v.envelope_level = 0.0

    def voice_samples(
        self, voice_idx: int, n: int, time_window_s: float | None = None
    ) -> np.ndarray:
        """Generate `n` samples of voice's waveform across `time_window_s`
        seconds. Returns float32 in [-1, 1] (envelope applied).

        Advances the voice's internal accumulator by n samples so successive
        calls produce continuous output. When time_window_s is None, falls
        back to n / SAMPLE_RATE. The caller owns the window: the scope's
        `VoiceScopeRenderer._voice_time_window_s` supplies one display frame
        of audio time under `time_base = "wallclock"` and `auto_cycles`
        periods of the voice's own frequency under `time_base = "auto"` —
        see SAMPLE_RATE."""
        v = self.voices[voice_idx]
        if v.is_silent():
            return np.zeros(n, dtype=np.float32)
        wave = primary_waveform(v.control)

        # The accumulator advances by freq per CPU cycle, so a sample covers
        # the window's clocks (clock * time_window_s) divided across n.
        if time_window_s is None:
            time_window_s = n / self.SAMPLE_RATE
        step_per_sample = v.freq * self.clock * time_window_s / n
        idx = np.arange(n, dtype=np.float64)
        accs = (v.accumulator + idx * step_per_sample) % ACCUMULATOR_RANGE
        v.accumulator = float((v.accumulator + n * step_per_sample) % ACCUMULATOR_RANGE)

        phases = accs / float(ACCUMULATOR_RANGE)  # in [0, 1)

        # Single waveform: the clean bipolar shape.
        if (v.control & WAVE_MASK) in (
            WAVE_TRIANGLE,
            WAVE_SAWTOOTH,
            WAVE_PULSE,
            WAVE_NOISE,
        ):
            out = self._waveform_unit(wave, phases, voice_idx) * 2.0 - 1.0
        else:
            # Combined waveform: the SID wires the selected waveforms' 12-bit
            # oscillator outputs onto a shared bus, bitwise-ANDing them.
            # Faithful in character, not chip-exact.
            combined = np.full(n, 0x0FFF, dtype=np.uint16)
            for bit in (WAVE_TRIANGLE, WAVE_SAWTOOTH, WAVE_PULSE, WAVE_NOISE):
                if v.control & bit:
                    unit = self._waveform_unit(bit, phases, voice_idx)
                    combined &= (unit * 0x0FFF).astype(np.uint16)
            out = combined.astype(np.float64) / 2047.5 - 1.0

        return (out * v.envelope_level).astype(np.float32)

    def _waveform_unit(self, bit: int, phases: np.ndarray, voice_idx: int) -> np.ndarray:
        """One selected waveform's oscillator output over `phases`, as a unit
        ramp in [0, 1] — the single place that knows what each SID waveform's
        shape is.

        Both of voice_samples' scalings derive from this and neither moves:
        the single-waveform trace is ``unit * 2 - 1`` and the combined path's
        unsigned 12-bit form is ``unit * 0x0FFF``, which are the exact
        expressions the two branches used to spell out separately (the
        triangle's bipolar form agrees to within one float64 ulp, far below
        the float32 the trace is returned in). Keeping both scalings — rather
        than deriving the bipolar trace from the truncated 12-bit one — is
        what preserves the byte-identical guarantee noted above."""
        v = self.voices[voice_idx]
        if bit == WAVE_SAWTOOTH:
            return phases
        if bit == WAVE_TRIANGLE:
            return 1.0 - np.abs(2.0 * phases - 1.0)
        if bit == WAVE_PULSE:
            pw_frac = max(1, v.pulse_width) / PULSE_WIDTH_RANGE
            return np.where(phases < pw_frac, 1.0, 0.0)
        # WAVE_NOISE
        rng = self._noise_rng[voice_idx]
        return np.array([rng.random() for _ in range(len(phases))], dtype=np.float64)
