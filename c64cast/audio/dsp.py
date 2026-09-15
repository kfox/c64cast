"""Host-side audio DSP for the 4-bit SID `$D418` DAC path.

Five composable, stateful processors — :class:`PreEmphasis`,
:class:`Expander`, :class:`Compressor`, :class:`Limiter`, :class:`AGC` —
wired into source-appropriate order by :class:`AudioDSP` and run on float
samples in [-1, 1] before `audio_handlers.encode_floats_to_dac` quantizes
them. Config surface is `[dsp]` via `config.DSPCfg`.

See docs/architecture/audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

_EPS = 1e-9

# HW-A/B-tuned on a real 6581 (2026-06-12); rationale in
# audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path.
PRE_EMPHASIS_MIC_DEFAULT = 0.7
PRE_EMPHASIS_LINE_DEFAULT = 0.6


class _Processor(Protocol):
    """Structural type for a DSP stage: stateful, block-fed, resettable."""

    def process(self, x: np.ndarray) -> np.ndarray: ...
    def reset(self) -> None: ...


def db_to_lin(db: float) -> float:
    """Decibels → linear amplitude ratio."""
    return float(10.0 ** (db / 20.0))


def lin_to_db(x: float) -> float:
    """Linear amplitude → decibels, floored so 0 maps to a large negative
    value instead of -inf (keeps gain math finite at true silence)."""
    return float(20.0 * np.log10(max(abs(x), _EPS)))


def _one_pole_coeff(time_s: float, sample_rate: int) -> float:
    """One-pole smoothing coefficient for the given time constant. coeff in
    [0, 1): ``y[n] = coeff*y[n-1] + (1-coeff)*x[n]``. time_s=0 → 0 (instant)."""
    t = max(float(time_s), 0.0)
    if t <= 0.0:
        return 0.0
    return float(np.exp(-1.0 / (t * sample_rate)))


def _ar_envelope(
    level: np.ndarray, atk: float, rel: float, init: float
) -> tuple[np.ndarray, float]:
    """Attack/release one-pole envelope follower over a 1-D level signal.

    Rising input uses the (fast) attack coefficient, falling input the (slow)
    release coefficient — the standard detector that catches transients quickly
    and decays gently. Returns the smoothed envelope and the carried final
    state so the next block continues seamlessly.
    """
    n = level.shape[0]
    out = np.empty(n, dtype=np.float32)
    e = float(init)
    for i in range(n):
        x = float(level[i])
        c = atk if x > e else rel
        e = c * e + (1.0 - c) * x
        out[i] = e
    return out, e


class PreEmphasis:
    """First-order high-frequency boost: ``y[n] = x[n] + amount*(x[n]-x[n-1])``.

    ``amount`` 0 = identity; larger = brighter. A constant (DC) signal is
    unchanged because its sample-to-sample difference is zero — only high
    frequencies are lifted. Carries one sample of state (the previous input)
    across blocks.
    """

    def __init__(self, amount: float):
        self.amount = float(amount)
        self._last = 0.0

    def reset(self) -> None:
        self._last = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x = x.astype(np.float32, copy=False)
        if self.amount == 0.0:
            self._last = float(x[-1])
            return x
        prev = np.empty_like(x)
        prev[0] = self._last
        prev[1:] = x[:-1]
        y = x + np.float32(self.amount) * (x - prev)
        self._last = float(x[-1])
        return np.asarray(y, dtype=np.float32)


class Compressor:
    """Soft-knee feed-forward compressor with makeup gain.

    Detector: an attack/release-smoothed peak envelope. Gain: a static dB curve
    (with optional soft knee) applied to the smoothed level, so above
    ``threshold_db`` the signal is reduced by ``(1 - 1/ratio)`` of its excess.
    ``makeup_db=None`` auto-computes makeup so a signal at the threshold exits
    near unity, restoring perceived loudness after the reduction.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        threshold_db: float,
        ratio: float,
        knee_db: float = 0.0,
        attack_ms: float = 5.0,
        release_ms: float = 120.0,
        makeup_db: float | None = None,
    ):
        self.sample_rate = sample_rate
        self.threshold_db = float(threshold_db)
        self.ratio = max(float(ratio), 1.0)
        self.knee_db = max(float(knee_db), 0.0)
        self._atk = _one_pole_coeff(attack_ms / 1000.0, sample_rate)
        self._rel = _one_pole_coeff(release_ms / 1000.0, sample_rate)
        if makeup_db is None:
            self.makeup_db = -self.threshold_db * (1.0 - 1.0 / self.ratio)
        else:
            self.makeup_db = float(makeup_db)
        self._env = 0.0

    def reset(self) -> None:
        self._env = 0.0

    def _gain_db(self, level_db: np.ndarray) -> np.ndarray:
        """Static gain-reduction curve (<= 0 dB) for a level array, soft knee."""
        over = level_db - self.threshold_db
        slope = 1.0 / self.ratio - 1.0
        if self.knee_db > 0.0:
            half = self.knee_db / 2.0
            knee = slope * np.square(over + half) / (2.0 * self.knee_db)
            above = slope * over
            gain = np.where(over <= -half, 0.0, np.where(over >= half, above, knee))
        else:
            gain = np.where(over > 0.0, slope * over, 0.0)
        return gain.astype(np.float32, copy=False)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x = x.astype(np.float32, copy=False)
        env, self._env = _ar_envelope(np.abs(x), self._atk, self._rel, self._env)
        level_db = 20.0 * np.log10(np.maximum(env, _EPS))
        gain_db = self._gain_db(level_db) + self.makeup_db
        gain = np.power(10.0, gain_db / 20.0).astype(np.float32)
        return (x * gain).astype(np.float32, copy=False)


class Limiter:
    """Fast peak limiter / brickwall ceiling.

    The detector uses instant attack (catches the peak the moment it arrives)
    and a release-smoothed recovery, so gain is pulled down only as far and as
    long as needed to keep the output under ``ceiling``. A final hard clip
    guards against intra-sample overshoot. Below the ceiling it is transparent.
    """

    def __init__(self, *, sample_rate: int, ceiling: float = 0.95, release_ms: float = 50.0):
        self.sample_rate = sample_rate
        self.ceiling = float(ceiling)
        self._rel = _one_pole_coeff(release_ms / 1000.0, sample_rate)
        self._env = 0.0

    def reset(self) -> None:
        self._env = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x = x.astype(np.float32, copy=False)
        env, self._env = _ar_envelope(np.abs(x), 0.0, self._rel, self._env)
        gain = np.where(env > self.ceiling, self.ceiling / np.maximum(env, _EPS), 1.0).astype(
            np.float32
        )
        y = x * gain
        return np.clip(y, -self.ceiling, self.ceiling).astype(np.float32, copy=False)


class Expander:
    """Downward expander with hysteresis — a gentler, chatter-free noise gate.

    Below ``threshold_db`` the signal is attenuated by ``(ratio-1)`` of how far
    it sits below threshold (down to ``floor_db`` of attenuation). Hysteresis:
    the gate opens at ``threshold_db`` but only closes once the level falls
    ``hysteresis_db`` below it, so a signal hovering at the threshold doesn't
    rapidly toggle. Gain changes are attack/release-smoothed (fast open, slow
    close).
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        threshold_db: float,
        ratio: float = 2.0,
        hysteresis_db: float = 6.0,
        floor_db: float = -60.0,
        attack_ms: float = 5.0,
        release_ms: float = 80.0,
    ):
        self.sample_rate = sample_rate
        self.open_db = float(threshold_db)
        self.close_db = float(threshold_db) - abs(float(hysteresis_db))
        self.ratio = max(float(ratio), 1.0)
        self.floor_gain = db_to_lin(floor_db)
        self._det_atk = _one_pole_coeff(attack_ms / 1000.0, sample_rate)
        self._det_rel = _one_pole_coeff(release_ms / 1000.0, sample_rate)
        self._g_atk = _one_pole_coeff(attack_ms / 1000.0, sample_rate)
        self._g_rel = _one_pole_coeff(release_ms / 1000.0, sample_rate)
        self._env = 0.0
        self._gain = 1.0
        self._open = False

    def reset(self) -> None:
        self._env = 0.0
        self._gain = 1.0
        self._open = False

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x = x.astype(np.float32, copy=False)
        n = x.shape[0]
        out = np.empty(n, dtype=np.float32)
        e = self._env
        g = self._gain
        is_open = self._open
        det_atk, det_rel = self._det_atk, self._det_rel
        g_atk, g_rel = self._g_atk, self._g_rel
        open_lin = db_to_lin(self.open_db)
        close_lin = db_to_lin(self.close_db)
        slope = self.ratio - 1.0
        thr_db = self.open_db
        floor = self.floor_gain
        for i in range(n):
            a = abs(float(x[i]))
            c = det_atk if a > e else det_rel
            e = c * e + (1.0 - c) * a
            if e >= open_lin:
                is_open = True
            elif e < close_lin:
                is_open = False
            if is_open:
                target = 1.0
            else:
                level_db = 20.0 * np.log10(max(e, _EPS))
                gain_db = slope * (level_db - thr_db)
                target = max(10.0 ** (gain_db / 20.0), floor)
            cg = g_atk if target > g else g_rel
            g = cg * g + (1.0 - cg) * target
            out[i] = float(x[i]) * g
        self._env = e
        self._gain = g
        self._open = is_open
        return out


class AGC:
    """Slow automatic gain control for the mic path.

    Tracks a smoothed RMS estimate of the input and nudges a single broadband
    gain toward the value that would bring that RMS to ``target_db``, bounded by
    ``±max_gain_db``. Input quieter than ``noise_floor_db`` is treated as
    silence — the gain is held rather than cranked up to amplify the noise floor.

    Known limitation: being level-based, AGC cannot tell a -30 dB noise floor
    from -30 dB quiet speech (measured 2026-06-12, Kaggle speech-noise set,
    scripts/diags/dsp_noise.py). See audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        target_db: float = -18.0,
        max_gain_db: float = 24.0,
        time_ms: float = 300.0,
        noise_floor_db: float = -60.0,
    ):
        self.sample_rate = sample_rate
        self.target_lin = db_to_lin(target_db)
        self.max_gain = db_to_lin(max_gain_db)
        self.min_gain = db_to_lin(-max_gain_db)
        self.noise_floor = db_to_lin(noise_floor_db)
        self.time_s = max(time_ms / 1000.0, 1e-3)
        self._rms = 0.0
        self._gain = 1.0

    def reset(self) -> None:
        self._rms = 0.0
        self._gain = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x = x.astype(np.float32, copy=False)
        n = x.shape[0]
        c = _one_pole_coeff(self.time_s, self.sample_rate)
        out = np.empty(n, dtype=np.float32)
        ms = self._rms * self._rms
        g = self._gain
        target = self.target_lin
        nf2 = self.noise_floor * self.noise_floor
        for i in range(n):
            s = float(x[i])
            ms = c * ms + (1.0 - c) * (s * s)
            if ms < nf2:
                desired = g
            else:
                desired = min(max(target / max(np.sqrt(ms), _EPS), self.min_gain), self.max_gain)
            g = c * g + (1.0 - c) * desired
            out[i] = s * g
        self._rms = float(np.sqrt(ms))
        self._gain = g
        return out


@dataclass
class DSPParams:
    """Pure DSP parameters (config metadata lives on ``config.DSPCfg``, which
    builds one of these). Defaults are tuned for the 4-bit DAC.

    ``pre_emphasis = None`` selects the source-aware auto default; a number
    forces that amount; 0.0 disables the stage. ``comp_makeup_db = None``
    auto-computes makeup gain."""

    enabled: bool = False
    pre_emphasis: float | None = None
    expander: bool = True
    expander_threshold_db: float = -45.0
    expander_ratio: float = 2.0
    expander_hysteresis_db: float = 6.0
    expander_floor_db: float = -60.0
    expander_attack_ms: float = 5.0
    expander_release_ms: float = 80.0
    compress: bool = True
    comp_threshold_db: float = -18.0
    comp_ratio: float = 3.0
    comp_knee_db: float = 6.0
    comp_attack_ms: float = 5.0
    comp_release_ms: float = 120.0
    comp_makeup_db: float | None = None
    limiter: bool = True
    limiter_ceiling: float = 0.95
    limiter_release_ms: float = 50.0
    agc: bool = False  # mic path only
    agc_target_db: float = -18.0
    agc_max_gain_db: float = 24.0
    agc_time_ms: float = 300.0
    agc_noise_floor_db: float = -60.0


class AudioDSP:
    """The enabled processors wired into source-appropriate order.

    Order: pre-emphasis → (AGC, mic only) → expander → compressor → limiter.
    Pre-emphasis shapes first; AGC normalizes gross mic level; the expander
    cleans the noise floor before the compressor's makeup would raise it; the
    compressor evens dynamics; the limiter is the final ceiling. A disabled
    chain (``enabled=False``) is an exact identity.
    """

    def __init__(self, params: DSPParams, *, sample_rate: int, is_mic: bool):
        self.params = params
        self.sample_rate = sample_rate
        self.is_mic = is_mic
        self._chain: list[_Processor] = []
        if not params.enabled:
            return
        pre = params.pre_emphasis
        if pre is None:
            pre = PRE_EMPHASIS_MIC_DEFAULT if is_mic else PRE_EMPHASIS_LINE_DEFAULT
        if pre > 0.0:
            self._chain.append(PreEmphasis(pre))
        if is_mic and params.agc:
            self._chain.append(
                AGC(
                    sample_rate=sample_rate,
                    target_db=params.agc_target_db,
                    max_gain_db=params.agc_max_gain_db,
                    time_ms=params.agc_time_ms,
                    noise_floor_db=params.agc_noise_floor_db,
                )
            )
        if params.expander:
            self._chain.append(
                Expander(
                    sample_rate=sample_rate,
                    threshold_db=params.expander_threshold_db,
                    ratio=params.expander_ratio,
                    hysteresis_db=params.expander_hysteresis_db,
                    floor_db=params.expander_floor_db,
                    attack_ms=params.expander_attack_ms,
                    release_ms=params.expander_release_ms,
                )
            )
        if params.compress:
            self._chain.append(
                Compressor(
                    sample_rate=sample_rate,
                    threshold_db=params.comp_threshold_db,
                    ratio=params.comp_ratio,
                    knee_db=params.comp_knee_db,
                    attack_ms=params.comp_attack_ms,
                    release_ms=params.comp_release_ms,
                    makeup_db=params.comp_makeup_db,
                )
            )
        if params.limiter:
            self._chain.append(
                Limiter(
                    sample_rate=sample_rate,
                    ceiling=params.limiter_ceiling,
                    release_ms=params.limiter_release_ms,
                )
            )

    @property
    def active(self) -> bool:
        """True when at least one processor will run (enabled + non-empty)."""
        return bool(self._chain)

    def reset(self) -> None:
        for proc in self._chain:
            proc.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self._chain or x.size == 0:
            return x
        y = x.astype(np.float32, copy=False)
        for proc in self._chain:
            y = proc.process(y)
        return y
