"""Minimal SID emulator for waveform *visualization*.

Not a full emulator — no filter, no master volume mixing, no per-cycle
6502 timing. The real SID chip plays the file (driven by the C64-side
player PRG that ``api.run_sid_player`` uploads) and produces the actual
audio; this module mirrors the per-voice waveform shape + ADSR envelope
based on a periodic snapshot of the SID's 25 registers ($D400-$D418)
that the WaveformScene polls from the U64.

Per-voice we track:
  * 24-bit phase accumulator (advanced internally — does not need to
    match the real chip's phase; the visualization just wants to draw
    a smoothly-advancing wave at the right frequency).
  * Waveform select bits + pulse width.
  * Envelope state machine driven by the gate bit (control bit 0).

For combined waveforms (multiple bits set in the upper nibble of the
control byte) we pick by priority: noise > pulse > sawtooth > triangle.
The real SID ANDs the bit patterns together; the result is a "metallic"
sound without a clean visual shape. Picking by priority gives a
recognizable trace per voice.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from .c64 import CLOCK_NTSC, CLOCK_PAL, SID

# Re-export the wave-select bit constants under their historical names so
# existing imports (`from c64cast.sidemu import WAVE_NOISE, ...`) keep
# working. Authoritative definitions live in c64.SID.
WAVE_TRIANGLE = SID.WAVE_TRIANGLE
WAVE_SAWTOOTH = SID.WAVE_SAWTOOTH
WAVE_PULSE    = SID.WAVE_PULSE
WAVE_NOISE    = SID.WAVE_NOISE
GATE          = SID.GATE

# SID ADSR rate table (in seconds to traverse full range, indexed by the
# 4-bit AD/SR nibble). Decay/Release uses 3× the attack time per the SID
# spec. From the resid reference.
ATTACK_TIMES_S = [
    0.002, 0.008, 0.016, 0.024, 0.038, 0.056, 0.068, 0.080,
    0.100, 0.250, 0.500, 0.800, 1.000, 3.000, 5.000, 8.000,
]
DECAY_TIMES_S = [t * 3.0 for t in ATTACK_TIMES_S]

NIBBLE_MASK = 0x0F             # mask off the low 4 bits of an AD/SR byte
NIBBLE_MAX = 15                # full-scale value of a 4-bit field
SID_REG_COUNT = 25             # $D400-$D418 (3 voices × 7 + 4 global)
ACCUMULATOR_BITS = 24          # SID phase accumulator width
ACCUMULATOR_RANGE = 1 << ACCUMULATOR_BITS
PULSE_WIDTH_RANGE = 4096       # 12-bit pulse-width register max + 1
NOISE_SEED_STRIDE = 1337       # arbitrary stride per voice; prime, stable
NOISE_SEED_OFFSET = 7


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
    freq: int = 0          # 16-bit
    pulse_width: int = 0   # 12-bit
    control: int = 0       # 8-bit
    ad: int = 0            # attack hi nibble, decay lo nibble
    sr: int = 0            # sustain hi nibble, release lo nibble

    # Emulator-internal state.
    accumulator: float = 0.0    # 24-bit phase, kept as float for ease
    envelope_level: float = 0.0
    envelope_state: str = "release"   # attack | decay | sustain | release
    noise_seed: int = 1                # per-voice LFSR-ish seed

    def gated(self) -> bool:
        return bool(self.control & GATE)


class SIDEmulator:
    """Stateful per-voice waveform generator.

    Usage from a scene:
        emu = SIDEmulator(system="NTSC")
        # each frame:
        regs = api.read_memory(0xD400, 25)
        emu.update_registers(regs)
        emu.advance_envelopes(dt_seconds)
        for v in range(3):
            samples = emu.voice_samples(v, n=320)   # float32 in [-1, 1]
    """

    # Fallback visualization rate used only when a caller of
    # voice_samples() doesn't pass time_window_s. WaveformScene always
    # passes one frame's worth of audio time, which locks the displayed
    # waveform phase to wall-clock (pitch changes you hear line up with
    # wave shape changes you see).
    SAMPLE_RATE = 22050

    def __init__(self, system: str = "NTSC"):
        self.clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
        self.voices: list[Voice] = [Voice() for _ in range(SID.N_VOICES)]
        # Per-voice deterministic noise sequences so the visualization
        # is stable across reset.
        self._noise_rng = [
            random.Random(i * NOISE_SEED_STRIDE + NOISE_SEED_OFFSET)
            for i in range(SID.N_VOICES)
        ]

    # ---- register snapshot --------------------------------------------------

    def update_registers(self, regs: bytes,
                         retrigger: tuple[bool, ...] | None = None):
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
            v.freq = (regs[base + SID.OFF_FREQ_LO]
                      | (regs[base + SID.OFF_FREQ_HI] << 8))
            v.pulse_width = (regs[base + SID.OFF_PW_LO]
                             | ((regs[base + SID.OFF_PW_HI] & NIBBLE_MASK) << 8))
            new_ctrl = regs[base + SID.OFF_CONTROL]
            was_gated = v.gated()
            v.control = new_ctrl
            now_gated = v.gated()
            if now_gated and not was_gated:
                v.envelope_state = "attack"
            elif was_gated and not now_gated:
                v.envelope_state = "release"
            if retrigger is not None and retrigger[v_idx]:
                # Hard restart: re-attack from zero (gate pulsed off→on
                # within the tick; the edge logic above couldn't see it).
                v.envelope_state = "attack"
                v.envelope_level = 0.0
            v.ad = regs[base + SID.OFF_AD]
            v.sr = regs[base + SID.OFF_SR]

    # ---- envelope time-stepping --------------------------------------------

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

    # ---- waveform generation -----------------------------------------------

    def voice_samples(self, voice_idx: int, n: int,
                      time_window_s: float | None = None) -> np.ndarray:
        """Generate `n` samples of voice's waveform across `time_window_s`
        seconds. Returns float32 in [-1, 1] (envelope applied).

        Advances the voice's internal accumulator by n samples so successive
        calls produce continuous output. When time_window_s is None, falls
        back to n / SAMPLE_RATE; WaveformScene passes 1/target_fps so one
        rendered row covers exactly one display frame of audio time."""
        v = self.voices[voice_idx]
        wave = primary_waveform(v.control)
        if wave == 0 or v.envelope_level <= 0.0:
            # Silent — return the resting zero line.
            return np.zeros(n, dtype=np.float32)

        # Per-sample CPU-clock advance: total clocks across the window
        # (clock * time_window_s) divided across n samples, then scaled
        # by the SID's freq register (accumulator += freq per CPU cycle).
        if time_window_s is None:
            time_window_s = n / self.SAMPLE_RATE
        step_per_sample = v.freq * self.clock * time_window_s / n
        # Build phase trajectory as float (modulo 2^24).
        idx = np.arange(n, dtype=np.float64)
        accs = (v.accumulator + idx * step_per_sample) % ACCUMULATOR_RANGE
        # Update stored accumulator past the last sample.
        v.accumulator = float(
            (v.accumulator + n * step_per_sample) % ACCUMULATOR_RANGE)

        phases = accs / float(ACCUMULATOR_RANGE)   # in [0, 1)

        if wave == WAVE_SAWTOOTH:
            out = 2.0 * phases - 1.0
        elif wave == WAVE_TRIANGLE:
            # /\ shape spanning [-1, 1].
            out = np.where(phases < 0.5,
                           4.0 * phases - 1.0,
                           3.0 - 4.0 * phases)
        elif wave == WAVE_PULSE:
            pw_frac = max(1, v.pulse_width) / PULSE_WIDTH_RANGE
            out = np.where(phases < pw_frac, 1.0, -1.0)
        else:   # WAVE_NOISE
            rng = self._noise_rng[voice_idx]
            out = np.array([rng.uniform(-1.0, 1.0) for _ in range(n)],
                           dtype=np.float64)

        return (out * v.envelope_level).astype(np.float32)
