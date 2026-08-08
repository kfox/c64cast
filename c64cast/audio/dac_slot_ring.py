"""The slot-ring measurement core for ``--calibrate-dac``: build the ``$D418``
code ring, read a capture of it back into signed per-code levels, and fold
those levels into the amplitude→code "sidtable" plus its quality metrics.

Everything here is pure numpy over an in-memory waveform — no hardware, no
sounddevice — so the whole pipeline, gates included, runs on synthetic
captures in tests. The run orchestration that produces real captures
(hardware bring-up, socket isolation, retries, persistence) is
:mod:`c64cast.audio.dac_calibration`; picking and probing the capture device is
:mod:`c64cast.audio.dac_capture_device`.

Measurement method: one slot ring, signed levels read directly
---------------------------------------------------------------
The SID → capture path is AC-coupled (~8.5 Hz measured), so a static code
produces no steady signal and a level can only be read as a *change*. The ring
therefore holds ``SLOT_SAMPLES``-long slots alternating ``[code][ref]``, with
``ref = $00`` — master volume 0, i.e. silence — behind a leading run of
``SYNC_SLOTS`` reference slots that marks where a pass begins. See
:func:`build_slot_ring`.

Every code is then measured against the *same* baseline inside one capture, so
its signed level comes straight off the waveform and no sign has to be inferred.
:func:`extract_slot_levels` locates the pass boundaries, tracks the slot grid
edge by edge, undoes the AC coupling, and differences each code slot against the
reference slots bracketing it. A ring holds 112 codes, so 256 codes take 3 rings
of ~5 s rather than 512 separate captures.

**This replaced a two-reference scheme** that toggled each code against ``$00``
*and* ``$0F`` at 500 Hz and took the FFT amplitude as ``|L(code) − L(ref)|``,
inferring each sign from which of ``p + q`` or ``q − p`` came closer to ``lmax``.
That primitive did not return consistent levels: on a 6581 whose filter path is
alive it missed the volume-0 ground truth below by 52 %, and 89 of its 256 codes
violated the triangle inequality ``p + q ≥ lmax`` by up to 51 % of ``lmax`` — so
no 1-D embedding of those numbers existed and the sign inference was not
ill-conditioned but unfounded. It was exactly reproducible (Pearson +0.9992
against a curve measured three weeks earlier), independent of toggle frequency
from 500 Hz down to 31.25 Hz, and reproduced within a single capture, ruling out
noise, drift, capture gain, clipping and stereo folding. Reading levels directly
sidesteps the whole construction; the same chip now passes the ground truth at
1.3 %.

Stereo capture is folded to mono by averaging, so the SID pan setting only
scales all measurements uniformly and cancels in the normalized ladder — no
mixer changes needed.

Context dependence, and why every code is measured three times
---------------------------------------------------------------
A 6581's output for a ``$D418`` byte is not quite a function of that byte alone.
Measured on a socketed 6581 by planting one probe code at twelve positions in an
otherwise ordinary ring: a positive code reads **20 % lower** at the end of a
ring pass than at its start, a negative code 2 % *higher*, and the apparent level
correlates at |r| ≈ 0.9 with the mean level of the surrounding slots. It is in
the raw waveform, before any processing, so it is the chip's operating point
sliding with the accumulated signal, not a measurement artifact.

Measure each code at one fixed slot and that bias is baked into the ladder,
ordered by code, looking exactly like curve structure — the tell is that the
volume ramp within a nibble band stops being monotone. So the whole code set is
measured :data:`MEASURE_ROUNDS` times, each round rotating every ring's slot
order by another fraction of a ring, and the readings are averaged: every code
then carries the same mean context, which is a common scale factor, and the
ladder is scale-invariant. Three rounds lands within one ladder step of a
six-round reference (max 0.9 % of span, rms 0.2 %); one round is off by 5.2 %
and leaves six non-monotone codes. ``context_spread_frac`` records how far a
code moved between rounds — the honest bound on how well *any* static table can
describe this chip.

The volume-0 self-test (why a calibration can be rejected)
----------------------------------------------------------
The 16 codes ``$h0`` set the master volume nibble to 0, so their output level is
``$00``'s *whatever* the upper nibble does: ``L($h0)`` must measure zero, for
every ``h``, with no model assumptions at all.
:func:`build_sidtable_from_levels` checks that and refuses to emit a ladder when
the worst one exceeds :data:`SELFTEST_TOLERANCE`; ``run_calibration`` then
persists ``raw_signed_levels`` + ``metrics`` for the socket but no ``sidtable``,
so playback degrades to the baked/linear curve instead of to a wrong table. That
matters because the failure is otherwise silent — a badly reconstructed ladder
looks exactly like a good one and plays back worse than no calibration at all.

On the socketed 6581 the residual is ~1 %, and it is not noise: it tracks the
filter routing bits (LP set → ≈ 1 % of full scale, no filter → ≈ 0.1 %) and does
not move when the plateau read window is widened from 8 to 72 samples, so it is
the chip's filter path leaking a little DC past a volume DAC set to zero rather
than anything the measurement is doing wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

import numpy as np

NMI_RATE = 8000  # consumer rate; well under the ~14 kHz NMI DAC handler ceiling
CAP_SR = 48000  # preferred capture rate (what a Cam Link presents)
REF_ZERO = 0x00  # master-volume-0 floor: the common baseline every level is vs.
ANCHOR_CODE = 0x0F  # positive full scale; first pair of every ring (see below)

#: NMI samples per ring slot. One slot must be long enough that its plateau is
#: many capture samples wide (32 NMI samples ≈ 4.0 ms ≈ 192 capture samples at
#: 48 kHz) and short enough that a useful number of codes fit one 8 KB ring.
SLOT_SAMPLES = 32

#: Leading run of ``REF_ZERO`` slots that marks the start of a ring pass. The
#: SID output is constant across it, so the capture shows no level steps for
#: ≈128 ms — the one feature in the waveform that cannot be confused with a
#: code boundary. See :func:`_find_ring_anchors`.
SYNC_SLOTS = 32

#: How far the volume-0 self-test may miss before a measurement is rejected, as
#: a fraction of ``lmax``. Codes ``$h0`` set the master volume nibble to 0, so
#: their output level *is* ``$00``'s whatever the upper nibble does: ``L($h0)``
#: must measure 0, for every ``h``, with no model assumptions at all. Any
#: deviation is measurement error against a known answer, or chip leakage.
#: A sound measurement lands around 1 % (a socketed 6581, where the residual
#: tracks the filter routing bits — see the module docstring), and the primitive
#: this replaced missed by 52 %, so 10 % separates the two with room to spare.
SELFTEST_TOLERANCE = 0.10

#: Peak capture amplitude (of float32 full scale) below which the input simply
#: isn't carrying the SID. A ring drives the SID between full-scale codes and
#: silence, so *any* correctly routed capture sees far more than this; below it,
#: re-recording is pointless and only the rig can be at fault.
SILENT_CAPTURE_PEAK = 0.002

#: Above this ``pass_spread_frac``, the capture is not the ring at all. A
#: recording of something else (a laptop microphone picking up room noise is the
#: one seen in the field) still yields *numbers*: the peak finder locks onto
#: noise, a couple of "sync markers" turn up, and the levels come back near zero
#: with the passes disagreeing by ~100 %. Ungated, those numbers merged into the
#: table and the run only fell over later — at whichever ring happened to find
#: fewer than two markers, with a traceback and 30 s of measuring already spent.
RING_SPREAD_NOT_THE_RING = 0.10

#: Above this ``pass_spread_frac``, the capture *is* the ring but the ring is not
#: replaying the same levels each pass, so a ladder fitted to them is wrong.
#:
#: Every pass of one capture drives the SID through identical codes, so a healthy
#: rig reads 0.01–0.2 %. Only :data:`RING_SPREAD_NOT_THE_RING` used to be
#: checked, which made this band — an order of magnitude above healthy, two
#: orders below "you recorded the room" — indistinguishable from a good
#: measurement. One run read 0.6–2.5 % against a chip that had measured
#: 0.01–0.08 % sixteen minutes earlier, and the table fitted to it agreed with
#: that earlier one on 95 of 256 entries (corr 0.565 — a worse mismatch than
#: handing a chip a *different* chip's table, which ``dac_curves.py`` measures at
#: corr 0.738 / ~29 % RMS level error). It was written to disk and ``auto``
#: preferred it over the baked table for every subsequent run, which lands as
#: signal-correlated distortion: inaudible over a quiet passage, gross hiss once
#: the material gets loud.
#:
#: What made that capture unsteady was never established — the two runs differed
#: in the link they used *and* in whether a second SID on the machine could be
#: switched out of the way (only a backend with a config API can do that), and
#: the file was deleted before the two could be separated. So this gate is
#: deliberately a statement about the *data*, not about any link: passes that
#: disagree by 1.85 % cannot yield a trustworthy ladder whatever the cause, and
#: a rig that reads in the healthy band is unaffected regardless of how it
#: connects. 0.5 % is 2.5× the healthy worst case; :data:`RING_ATTEMPTS` still
#: absorbs a transient.
RING_TRUST_MAX_SPREAD = 0.005

#: Top of the healthy ``pass_spread_frac`` band, for the per-ring progress line.
#: A ring between this and :data:`RING_TRUST_MAX_SPREAD` is measured and kept,
#: but is worth saying out loud — a whole run sitting in that band is how a
#: quietly poor table gets built out of individually-passing rings.
RING_SPREAD_HEALTHY = 0.002

#: Fraction of ``pass_spread_frac`` that can survive :func:`_pass_gain_decomposition`
#: and still count as "only the level was moving". Below it the disagreement is a
#: per-pass gain — the ring replayed faithfully and was measured through something
#: that changed level; above it the laps genuinely differ and rescaling won't fix
#: them. Measured on synthetic captures the two land far apart: pure drift (a 2–20 %
#: ramp, or a settling envelope) reads 0.01–0.22, random per-block gain jitter reads
#: 0.70–0.89. 0.5 sits in the gap with room on both sides.
PASS_RESIDUAL_DRIFT_RATIO = 0.5

#: Captures per ring before giving up. A retry costs one settle + one capture
#: window (~5 s) and rescues a ring spoiled by a transient — a USB hiccup, a
#: host stall long enough to break the grid tracking — without letting a
#: genuinely wrong rig grind through the whole 9-ring run.
RING_ATTEMPTS = 2


class MeasurementError(RuntimeError):
    """The capture doesn't contain a readable slot ring — no signal, too short,
    or the NMI isn't running. Distinct from a measurement that read fine and
    then failed its self-test, which is a chip/primitive result, not a rig fault."""


class UnsteadyRingError(MeasurementError):
    """The capture *does* contain the ring, but its levels move between passes.

    A separate type because the advice inverts: every cause listed for a plain
    :class:`MeasurementError` is about the recording not carrying the ring, and
    sending someone to re-cable an input that is already correct is worse than
    saying nothing. Here the input is right and the ring is playing — what needs
    finding is whatever else is moving the level.

    Carries the capture's ``diagnostics`` so the failure can name *which* way it
    was unsteady (:func:`_pass_gain_decomposition`) rather than only how much."""

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics: dict[str, Any] = diagnostics or {}


def codes_per_ring(ring_size: int) -> int:
    """How many codes one ring can carry, *including* the leading anchor pair."""
    return (ring_size // SLOT_SAMPLES - SYNC_SLOTS) // 2


def plan_code_batches(per_ring: int, total: int = 256) -> list[list[int]]:
    """Split the codes across the fewest rings that hold them, striding rather
    than slicing: ring ``j`` gets ``j, j+n, j+2n, …``.

    Consecutive ring slots then hold codes whose *volume nibble* differs by the
    stride, so a chip that is silent over some contiguous run of codes can never
    put a long silent run into consecutive slots — which is what would fake a
    sync gap for :func:`_find_ring_anchors`. Slicing 0-110 / 111-221 / 222-255
    puts sixteen same-upper-nibble codes side by side and does exactly that."""
    n = max(1, -(-total // per_ring))
    return [list(range(j, total, n)) for j in range(n)]


#: How many times the whole code set is measured, each round rotating every
#: ring's slot order by a further ``1/MEASURE_ROUNDS`` of a ring.
#:
#: A 6581's output for a given ``$D418`` byte is not quite a function of that
#: byte alone: it drifts with the *accumulated* signal since the last quiet
#: stretch, as the parked voices' operating point slides. Measured on a socketed
#: 6581 (see docs/architecture/audio.md): a positive code reads 20 % lower at the
#: end of a ring pass than at the start, a negative code 2 % higher, and the
#: apparent level correlates at |r| ≈ 0.9 with the mean level of the surrounding
#: slots. Measure every code at one fixed position and that bias is baked into
#: the ladder, ordered by code, looking exactly like curve structure.
#:
#: Rotating the slot order and averaging gives every code the same *mean*
#: context, so the bias becomes a common scale factor — and the ladder is built
#: from a linspace over the measured span, so a common scale factor cancels
#: completely. The spread across rounds is kept as ``context_spread_frac``.
MEASURE_ROUNDS = 3


def plan_capture_rounds(
    per_ring: int, total: int = 256, rounds: int = MEASURE_ROUNDS
) -> list[list[list[int]]]:
    """``rounds`` × rings × codes. Each round rotates every ring's slot order by
    another ``1/rounds`` of the ring, so each code visits ``rounds`` evenly
    spaced positions across the runs. See :data:`MEASURE_ROUNDS`."""
    base = plan_code_batches(per_ring, total)
    out = []
    for r in range(rounds):
        out.append([b[(r * len(b)) // rounds :] + b[: (r * len(b)) // rounds] for b in base])
    return out


def build_slot_ring(codes: Sequence[int], ring_size: int, *, ref: int = REF_ZERO) -> bytes:
    """Ring holding ``SYNC_SLOTS`` reference slots followed by one
    ``[code][ref]`` slot pair per entry of `codes`, padded out with `ref`.

    Slot ``s`` occupies ring bytes ``[s·SLOT_SAMPLES, (s+1)·SLOT_SAMPLES)``, so
    every code is bracketed by the *same* reference level on both sides. Bytes
    are FULL 8-bit ``$D418`` values; the pattern tiles the ring exactly, so the
    NMI handler loops it with no wrap glitch."""
    slots = ring_size // SLOT_SAMPLES
    if len(codes) > codes_per_ring(ring_size):
        raise ValueError(f"{len(codes)} codes exceed the {codes_per_ring(ring_size)} a ring holds")
    seq = [ref & 0xFF] * SYNC_SLOTS
    for c in codes:
        seq += [int(c) & 0xFF, ref & 0xFF]
    seq += [ref & 0xFF] * (slots - len(seq))
    return np.repeat(np.asarray(seq, dtype=np.uint8), SLOT_SAMPLES).tobytes()


def _boxcar_step(x: np.ndarray, half: int) -> np.ndarray:
    """``s[n] = mean(x[n:n+half]) − mean(x[n−half:n])`` — a matched filter for a
    level step. ``|s|`` has a sharp triangular peak centered exactly on each
    boundary and is flat-ish elsewhere, which is what the alignment keys off."""
    n = x.size
    c = np.concatenate(([0.0], np.cumsum(x)))
    idx = np.arange(n)
    lo = np.clip(idx - half, 0, n)
    hi = np.clip(idx + half, 0, n)
    before = (c[idx] - c[lo]) / np.maximum(idx - lo, 1)
    after = (c[hi] - c[idx]) / np.maximum(hi - idx, 1)
    return after - before


def _peak_positions(mag: np.ndarray, min_sep: float, thresh: float) -> np.ndarray:
    """Positions of well-separated local maxima of `mag` above `thresh`, with
    a parabolic sub-sample refinement. Greedy non-maximum suppression: take the
    largest remaining peak, blank ``±min_sep`` around it, repeat."""
    cand = np.flatnonzero(mag > thresh)
    if cand.size == 0:
        return np.zeros(0)
    order = cand[np.argsort(mag[cand])[::-1]]
    taken = np.zeros(mag.size, dtype=bool)
    sep = max(1, int(min_sep))
    out: list[float] = []
    for i in order:
        if taken[i]:
            continue
        taken[max(0, i - sep) : i + sep + 1] = True
        pos = float(i)
        if 0 < i < mag.size - 1:
            a, b, c = mag[i - 1], mag[i], mag[i + 1]
            denom = a - 2 * b + c
            if denom < 0:
                pos += float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))
        out.append(pos)
    return np.sort(np.asarray(out))


def _find_ring_anchors(peaks: np.ndarray, slot_p: float) -> np.ndarray:
    """Ring-pass start positions: the edges that end a sync gap. The gap is
    ``SYNC_SLOTS`` constant-reference slots, so it is the longest stretch of the
    waveform with no level step in it; the edge that ends it is the
    ``ref → ANCHOR_CODE`` transition into slot ``SYNC_SLOTS``, guaranteed full
    scale and therefore never missed by the peak finder.

    A run of codes that all happen to sit at the reference level also leaves no
    edges, and on a chip whose Mahoney path has partly died that run can be
    long. So the test is *relative*: a candidate must be within 25 % of the
    longest gap seen, not merely long. Mistaking such a run for the marker
    would offset every code by a fixed number of slots and still repeat
    identically each pass — a wrong answer that looks perfectly stable."""
    if peaks.size < 2:
        return np.zeros(0)
    gaps = np.diff(peaks)
    floor = (SYNC_SLOTS - 8) * slot_p
    if gaps.max() < floor:
        return np.zeros(0)
    return peaks[1:][gaps >= max(floor, 0.75 * float(gaps.max()))]


def _fit_period(anchors: np.ndarray, nominal: float) -> tuple[float, float, float]:
    """Least-squares ``anchor_k = t0 + k·period`` over the observed pass starts,
    with ``k`` recovered from the nominal period. Returns (t0, period, rms)."""
    k = np.round((anchors - anchors[0]) / nominal)
    fit = np.polyfit(k, anchors, 1) if k.size > 1 else np.array([nominal, anchors[0]])
    resid = anchors - np.polyval(fit, k)
    return float(fit[1]), float(fit[0]), float(np.sqrt(np.mean(resid**2)))


def _dc_restore_gain(x: np.ndarray, c: np.ndarray, windows: np.ndarray) -> float:
    """The gain ``k`` that best undoes the capture path's AC coupling.

    The SID → capture path is high-passed (measured ≈8.5 Hz, τ ≈ 19 ms), so a
    plateau that should be flat sags visibly across one 4 ms slot and the naive
    "level = plateau mean" is biased by whatever the previous slots did. For a
    one-pole high-pass ``y = v − b`` with ``ḃ = y/τ``, the inverse is exactly
    ``v = y + cumsum(y)/(τ·fs)`` — one unknown scalar.

    Fit it from the data instead of trusting a nominal τ: the restored signal
    is affine in ``k``, so the total within-plateau variance is a quadratic in
    ``k`` with a closed-form minimum. `c` is ``cumsum(x)``; `windows` is an
    (n, w) index array of plateau interiors, which *should* be flat."""
    xw = x[windows]
    cw = c[windows]
    xw = xw - xw.mean(axis=1, keepdims=True)
    cw = cw - cw.mean(axis=1, keepdims=True)
    denom = float(np.sum(cw * cw))
    if denom <= 0:
        return 0.0
    return -float(np.sum(xw * cw)) / denom


@dataclass(frozen=True)
class SlotLevels:
    """Signed per-code output levels recovered from one slot-ring capture."""

    levels: np.ndarray  # (n_codes,) mean across ring passes, ref level = 0
    per_pass: np.ndarray  # (n_passes, n_codes) — spread here is the trust metric
    diagnostics: dict[str, Any]


#: Percentile of the per-code pass spread used as the trust statistic
#: (``pass_spread_p95_frac``, and the residual it is compared against). A max
#: over the ~86 codes of a ring is pinned by one transient glitch — on every
#: refused capture examined, 1–6 codes read far off on exactly one pass while
#: the rest agreed to 0.004 % — and gating on it failed good runs. 95 sits
#: above the glitch fraction those captures show (≤7 % of codes) while a ring
#: that genuinely is not replaying still moves every code, and this with it.
#: The diagnostics key bakes the value into its name — rename it if this is
#: ever tuned.
_SPREAD_TRUST_PERCENTILE = 95


def _pass_gain_decomposition(
    passes: np.ndarray, levels: np.ndarray, scale_ref: float
) -> tuple[np.ndarray, float]:
    """Split the pass-to-pass disagreement into a per-pass level change and
    whatever is left once that is taken out.

    ``pass_spread_frac`` says the passes disagree but not why, and the two causes
    want opposite advice. A capture whose level is still settling — or whose path
    changes gain mid-window — makes *faithful* passes read differently, purely
    because each was measured at a different point on the ramp; the ladder's shape
    is intact and a re-measure on a settled path fixes it. A ring that genuinely
    plays different levels each lap is wrong in a way no rescaling repairs, and
    points at something else reaching the output.

    Fitting one scalar per pass separates them: ``g_p`` is the level the whole ring
    came back at on lap ``p``, and the residual ``passes − g_p·levels`` is the part
    no single gain explains. It is deliberately reported in the same units as
    ``pass_spread_frac`` (max per-code std over ``scale_ref``) so the two compare
    directly — a residual well under the spread means a gain change accounts for
    it. Both failure modes reach the same spread otherwise: on synthetic captures
    a 10 % drift across the window and 1 % random per-block gain jitter each read
    1.8 %, and differ only here.
    """
    denom = float(levels @ levels)
    if denom <= 0 or passes.shape[0] < 2:
        return np.ones(passes.shape[0]), 0.0
    gains = np.asarray(passes @ levels, dtype=np.float64) / denom
    resid = passes - gains[:, None] * levels
    return gains, float(np.percentile(resid.std(axis=0), _SPREAD_TRUST_PERCENTILE)) / scale_ref


#: Half-width of the step-detector boxcar (:func:`_boxcar_step`), as a fraction
#: of the slot period. Its |response| to one boundary is a triangle two
#: half-widths wide, so anything ≤0.5 keeps adjacent boundaries' peaks from
#: overlapping; 0.4 takes that with margin while still averaging most of each
#: plateau into the step estimate.
_STEP_BOXCAR_HALF_FRAC = 0.4

#: An edge counts as a peak only above this fraction of the capture's strongest
#: step magnitudes. The reference is a high percentile rather than the max so a
#: single spike cannot set the bar; the fraction sits far below 1 because a
#: code near the reference level steps at a tiny fraction of the full-scale
#: anchor edge, yet above the flat-ish response between boundaries.
_STEP_PEAK_THRESH_FRAC = 0.15
_STEP_MAG_REF_PERCENTILE = 99.5

#: Non-maximum-suppression radius for :func:`_peak_positions`, as a fraction of
#: the slot period: real boundaries are at least one slot apart, so blanking
#: half a slot around each accepted peak drops double-detections of one edge
#: and can never swallow a genuine neighbor.
_STEP_PEAK_MIN_SEP_FRAC = 0.5

#: Capture samples trimmed from each end of a slot before its plateau is
#: averaged, keeping the boundary transition and its settling out of the mean.
#: At 48 kHz a slot is ≈192 samples, so 24 (≈0.5 ms) each side leaves a
#: ≈144-sample core; a pass whose tracked pitch leaves less than an 8-sample
#: core after trimming is dropped instead.
_SLOT_EDGE_GUARD_SAMPLES = 24


def extract_slot_levels(
    cap: np.ndarray,
    n_codes: int,
    ring_size: int,
    *,
    sr: int = CAP_SR,
    nmi_rate: float = NMI_RATE,
    guard: int = _SLOT_EDGE_GUARD_SAMPLES,
) -> SlotLevels:
    """Recover each code's *signed* output level, relative to the reference
    slots, from a capture of the ring :func:`build_slot_ring` built.

    Every code shares one baseline inside one capture, so a level is read
    directly off the waveform and its sign needs no inference — that is the
    whole point of the slot ring. The steps are:

    1. **Locate the boundaries.** ``|_boxcar_step|`` peaks on every level step;
       the peaks that follow a sync gap start a ring pass (:func:`_find_ring_anchors`).
    2. **Track the grid.** The pass period comes from a least-squares fit across
       the observed pass starts, and each pass then follows its own edges
       (:func:`_track_slot_grid`) rather than stepping a nominal pitch — see
       that function for why open-loop indexing sank two earlier attempts.
    3. **Undo the AC coupling** (:func:`_dc_restore_gain`) so a plateau mean is
       a level rather than a level plus the sag of whatever preceded it.
    4. **Difference against the neighbors.** Each code slot is bracketed by
       reference slots, so ``level = mean(code) − mean(both neighbors)/2``
       cancels any residual slow drift locally.
    """
    x = np.asarray(cap, dtype=np.float64)
    x = x - x.mean()
    ring_slots = ring_size // SLOT_SAMPLES
    slot_p = SLOT_SAMPLES * sr / float(nmi_rate)
    ring_p = ring_slots * slot_p

    step = _boxcar_step(x, max(2, int(round(_STEP_BOXCAR_HALF_FRAC * slot_p))))
    mag = np.abs(step)
    thresh = _STEP_PEAK_THRESH_FRAC * float(np.percentile(mag, _STEP_MAG_REF_PERCENTILE))
    peaks = _peak_positions(mag, _STEP_PEAK_MIN_SEP_FRAC * slot_p, thresh)
    anchors = _find_ring_anchors(peaks, slot_p)
    if anchors.size < 2:
        raise MeasurementError(
            f"found {anchors.size} ring sync marker(s) in the capture, need ≥2 — "
            "it holds no readable ring pass"
        )
    _, ring_p, anchor_rms = _fit_period(anchors, ring_p)
    slot_p = ring_p / ring_slots
    geom = RingGeometry(slot_period=slot_p, ring_slots=ring_slots, n_codes=n_codes)

    # Code i occupies slot SYNC_SLOTS + 2i, bracketed by reference slots.
    code_slots = SYNC_SLOTS + 2 * np.arange(n_codes)

    cum = np.cumsum(x)
    per_pass: list[np.ndarray] = []
    pitches: list[float] = []
    dc_gains: list[float] = []
    for a in anchors:
        starts, pitch = _track_slot_grid(peaks, a, geom)
        w = int(pitch) - 2 * guard
        if w < 8:
            continue
        first = int(np.floor(starts[0])) + guard
        last = int(np.ceil(starts[ring_slots - 1])) + guard + w
        if first < 0 or last >= x.size:
            continue
        idx = np.round(starts[:ring_slots, None] + guard).astype(int) + np.arange(w)
        k = _dc_restore_gain(x, cum, idx)
        dc_gains.append(k)
        pitches.append(pitch)
        means = (x[idx] + cum[idx] * k).mean(axis=1)
        ref_mean = 0.5 * (means[code_slots - 1] + means[code_slots + 1])
        per_pass.append(means[code_slots] - ref_mean)
    if not per_pass:
        raise MeasurementError("no complete ring pass fell inside the capture window")

    passes = np.vstack(per_pass)
    # Median, not mean, across passes. Individual slots glitch: on every refused
    # capture examined, 1-6 codes out of ~86 read far off on exactly one pass
    # while the other 80-odd agreed to 0.004%. A mean folds that outlier into the
    # code's level and the error survives into the ladder — which is what a wrong
    # entry sounds like. With three passes the median discards it outright.
    levels = np.median(passes, axis=0) if passes.shape[0] >= 3 else passes.mean(axis=0)
    scale_ref = float(np.max(np.abs(levels))) or 1.0
    tracked_slot_p = float(np.mean(pitches))
    gains, residual_frac = _pass_gain_decomposition(passes, levels, scale_ref)
    diagnostics = {
        "passes": int(passes.shape[0]),
        "ring_period_samples": round(ring_p, 3),
        "slot_period_samples": round(tracked_slot_p, 4),
        "nmi_rate_implied_hz": round(SLOT_SAMPLES * sr / tracked_slot_p, 2),
        "anchor_fit_rms_samples": round(anchor_rms, 2),
        # Worst single slot. Kept because it names the biggest error in the ring,
        # but it is NOT the trust metric: it is a max over ~86 codes, so one
        # transient glitch out of ~260 readings pins it at 1-2% while the ring as
        # a whole is replaying to 0.004%. Gating on it failed good runs.
        "pass_spread_frac": round(float(np.max(passes.std(axis=0))) / scale_ref, 5),
        # The trust metric: the 95th percentile of the same per-code spread. A
        # handful of glitched slots cannot move it, but a ring that genuinely is
        # not replaying moves every code and so moves this too.
        "pass_spread_p95_frac": round(
            float(np.percentile(passes.std(axis=0), _SPREAD_TRUST_PERCENTILE)) / scale_ref, 5
        ),
        "pass_outlier_codes": int((passes.std(axis=0) / scale_ref > RING_SPREAD_HEALTHY).sum()),
        # What that spread is made of (:func:`_pass_gain_decomposition`). The level
        # the whole ring came back at on each lap, how far those move, and the
        # disagreement still there once each lap is rescaled to the others —
        # a residual well under pass_spread_frac means the ring replayed fine and
        # only the level it was measured through was moving.
        "pass_gains": [round(float(g), 5) for g in gains],
        "pass_gain_span_frac": round(float(np.max(gains) - np.min(gains)), 5),
        "pass_residual_frac": round(residual_frac, 5),
        # The fitted AC-coupling corner, as a sanity check on the capture rig:
        # k = 1/(τ·fs), so f_c = k·fs/2π. Expect a few Hz to a few tens of Hz.
        "ac_coupling_hz": round(float(np.mean(dc_gains)) * sr / (2 * np.pi), 2),
    }
    return SlotLevels(levels=levels, per_pass=passes, diagnostics=diagnostics)


def read_ring_capture(
    cap: np.ndarray, n_codes: int, ring_size: int, *, sr: int = CAP_SR
) -> SlotLevels:
    """:func:`extract_slot_levels` behind the two gates that decide whether the
    capture is of the ring *at all*, rather than of something else entirely.

    The extraction is a reader: handed a waveform it reports what it found, and
    it can only refuse what it cannot parse. But a recording of the wrong input
    parses fine — the peak finder locks onto noise, a sync gap or two turns up,
    and levels come back near zero with the passes contradicting each other. So
    the judgment about whether a *recording* is usable lives here, where it can
    be applied to every ring before its numbers reach the table.

    Raises :class:`MeasurementError` whose message is a phrase, for
    :func:`c64cast.audio.dac_capture_device.capture_fault_message` to finish with the device and the advice.
    """
    peak = float(np.max(np.abs(cap))) if cap.size else 0.0
    if peak < SILENT_CAPTURE_PEAK:
        raise MeasurementError("it recorded silence")
    got = extract_slot_levels(cap, n_codes, ring_size, sr=sr)
    spread = float(got.diagnostics["pass_spread_p95_frac"])
    if spread > RING_SPREAD_NOT_THE_RING:
        raise MeasurementError(
            f"its ring passes disagree by {spread * 100:.1f}%, where hardware "
            "reads 0.01–0.2% — the levels in it are noise"
        )
    if spread > RING_TRUST_MAX_SPREAD:
        raise UnsteadyRingError(
            f"its ring passes disagree by {spread * 100:.2f}%, where hardware "
            "reads 0.01–0.2% — the capture is the ring, but the ring is not "
            "replaying the same levels each pass, so a ladder fitted to them "
            "would be wrong",
            got.diagnostics,
        )
    return got


class RingGeometry(NamedTuple):
    """One ring's layout as it appears in a capture: the fitted slot period in
    capture samples, the slot count of the whole ring, and how many code
    slots it carries."""

    slot_period: float
    ring_slots: int
    n_codes: int


#: Alpha-beta gains for :func:`_track_slot_grid`. Alpha smooths the ±½-sample
#: jitter in a single edge position; beta learns a *rate* of drift, which is
#: what keeps the tracker from lagging when the capture timebase is stretched
#: (avfoundation dropping samples under load). A pure position tracker at these
#: gains lags a 12 %-drop stretch by ~15 samples; with the rate term it doesn't.
_TRACK_ALPHA = 0.5
_TRACK_BETA = 0.1

#: How far from its prediction an edge may be found before the tracker calls it
#: a miss and coasts. Wide enough to acquire a badly stretched timebase, narrow
#: enough that it can never latch onto the *neighboring* boundary.
_TRACK_CAPTURE_FRAC = 0.35


def _track_slot_grid(
    peaks: np.ndarray, anchor: float, geom: RingGeometry
) -> tuple[np.ndarray, float]:
    """Start position of every slot in one ring pass, tracked edge by edge.

    This is the part two earlier attempts got wrong, so it is worth being
    explicit about why open-loop indexing cannot work. A slot is 192.24 capture
    samples — not an integer, and not even a fixed number, because the capture
    clock and the C64's NMI clock are independent and avfoundation drops
    samples under load. Stepping a nominal 192 samples per slot from the sync
    marker walks the read window off the boundary and into the middle of a
    sagging plateau within a fraction of a pass, which produces levels that are
    stable across repeats (so they look trustworthy) and wrong.

    So the grid follows the signal instead of predicting it: each boundary is
    matched to the nearest detected edge, and an alpha-beta filter folds that
    into a smoothed offset *and* a drift rate. Boundaries with no detectable
    edge — a code whose level happens to equal the reference, and the whole
    sync gap — coast on the current rate. Returns the slot starts plus the
    median tracked slot length."""
    starts = np.empty(geom.ring_slots + 1)
    last_edge_slot = SYNC_SLOTS + 2 * geom.n_codes
    off = 0.0
    rate = 0.0
    for s in range(SYNC_SLOTS, geom.ring_slots + 1):
        pred = anchor + (s - SYNC_SLOTS) * geom.slot_period + off
        if s < last_edge_slot and peaks.size:
            j = int(np.searchsorted(peaks, pred))
            near = peaks[max(0, j - 1) : j + 1]
            if near.size:
                d = min((p - pred for p in near), key=abs)
                if abs(d) < _TRACK_CAPTURE_FRAC * geom.slot_period:
                    off += _TRACK_ALPHA * d
                    rate += _TRACK_BETA * d
                    pred += _TRACK_ALPHA * d
        off += rate
        starts[s] = pred
    # The sync gap carries no edges to track, so extrapolate backwards from the
    # anchor at the nominal pitch. Only the single ref slot at SYNC_SLOTS-1 is
    # ever read (it brackets the first code), one slot from the anchor.
    for s in range(SYNC_SLOTS - 1, -1, -1):
        starts[s] = anchor - (SYNC_SLOTS - s) * geom.slot_period
    return starts, float(np.median(np.diff(starts[SYNC_SLOTS:])))


def merge_measurements(
    measured: Sequence[tuple[Sequence[int], SlotLevels]],
) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    """Fold every captured ring into one 256-entry signed level table.

    Two corrections, in order:

    * **Anchor rescale.** Every ring leads with :data:`ANCHOR_CODE` at the same
      slot, so each capture carries its own full-scale reading. Capture gain is
      stable *within* a capture but not guaranteed across them, so each ring is
      rescaled onto the mean anchor. That is what lets several rings stand in
      for the one 256-code ring that does not fit.
    * **Round average.** Each code is measured once per rotation
      (:func:`plan_capture_rounds`); averaging equalises the context bias
      described at :data:`MEASURE_ROUNDS`.

    ``context_spread_frac`` — how far a code's readings move between rotations,
    as a fraction of full scale — is the honest measure of how well a single
    static table can describe this chip at all."""
    anchors = np.array([m.levels[0] for _, m in measured])
    gain = float(np.mean(anchors))
    seen: dict[int, list[float]] = {}
    for codes, got in measured:
        k = gain / float(got.levels[0]) if got.levels[0] else 1.0
        for c, v in zip(codes, got.levels[1:], strict=True):
            seen.setdefault(int(c), []).append(float(v) * k)
    raw = [(c, float(np.mean(seen[c]))) for c in sorted(seen)]
    spreads = [float(np.ptp(v)) for v in seen.values() if len(v) > 1]
    metrics = {
        "rings": len(measured),
        "anchor_spread_frac": round(
            float(np.max(np.abs(anchors / gain - 1.0))) if gain else 0.0, 5
        ),
        "context_spread_frac": round(max(spreads) / abs(gain), 4) if spreads and gain else 0.0,
        "context_spread_median_frac": (
            round(float(np.median(spreads)) / abs(gain), 4) if spreads and gain else 0.0
        ),
    }
    return raw, metrics


def build_sidtable_from_levels(
    raw: Sequence[tuple[int, float]],
) -> tuple[list[int] | None, dict[str, Any]]:
    """Fold 256 measured signed output levels into the amplitude→code sidtable
    and its quality metrics.

    The levels arrive signed and on one common baseline (``L($00) = 0``) — the
    slot ring reads them straight off the waveform — so there is nothing to
    reconstruct here: the table maps 256 uniform target levels across the
    measured span to the code whose level is nearest.

    The targets span ``[min, max]`` rather than being centered on silence, and
    that is deliberate. What the encoder needs is *uniformity* — index ``128+k``
    must sit ``k`` equal steps above index 128 — not that index 128 be silent:
    the SID output is AC-coupled, so a constant offset is removed downstream and
    only the step size reaches the listener. Measured Mahoney spans are markedly
    asymmetric (socket 1: −0.656 to +0.461), and re-centring on zero would throw
    away the excess negative swing for nothing. It also has to work for a chip
    whose span is entirely one-sided — the degraded 6581 in socket 2 measures
    −0.001 to +0.287, where the largest symmetric swing is 0.001 and a
    zero-centered ladder collapses to noise.

    Returns ``(None, metrics)`` when the volume-0 self-test misses by more than
    :data:`SELFTEST_TOLERANCE`. Codes ``$h0`` set the master volume nibble to 0,
    so ``L($h0)`` must be 0 whatever the upper nibble does; a measurement that
    says otherwise is wrong about levels in general, and a ladder folded from it
    would look exactly like a good one while playing back worse than no
    calibration at all. Metrics are returned either way, so a rejected
    measurement stays fully diagnosable."""
    code = np.array([c for c, _ in raw])
    level = np.array([v for _, v in raw], dtype=np.float64)
    lmax = (
        float(level[code == ANCHOR_CODE][0]) if np.any(code == ANCHOR_CODE) else float(level.max())
    )
    scale = abs(lmax) or float(np.max(np.abs(level))) or 1.0

    # Ground truth, no model assumptions: master volume 0 is silence.
    at_vol0 = (code & 0x0F) == 0
    selftest = level[at_vol0] / scale
    worst = float(np.max(np.abs(selftest))) if selftest.size else 0.0

    lo, hi = float(level.min()), float(level.max())
    span = hi - lo
    targets = np.linspace(lo, hi, 256)
    sidtable = [int(code[np.argmin(np.abs(level - t))]) for t in targets]

    metrics: dict[str, Any] = {
        "signed_span": [round(lo, 6), round(hi, 6)],
        "lmax": round(lmax, 6),
        "volume0_selftest_worst": round(worst, 4),
        "volume0_selftest": [round(float(e), 4) for e in selftest],
        **_ladder_metrics(np.array([float(level[code == c][0]) for c in sidtable]), targets, span),
    }
    if worst > SELFTEST_TOLERANCE:
        return None, metrics
    return sidtable, metrics


def _ladder_metrics(achieved: np.ndarray, targets: np.ndarray, span: float) -> dict[str, float]:
    """Honest, capture-independent quality figures for a finished ladder.

    The metrics this replaced counted level differences exceeding the *capture
    noise floor*, which measured the recording rig rather than the DAC: across
    three runs a quieter capture scored 6.55 → 7.52 → 7.92 "effective bits" on
    the same hardware, rating a chip whose Mahoney path had degraded to roughly
    4 bits (7.52) above a working one (6.6). Everything here is a property of
    the reconstructed ladder alone.

    * ``ladder_bits`` — ENOB-style: the RMS distance between each of the 256
      requested target levels and the level actually achieved, expressed as the
      uniform quantizer that would have the same RMS error.
    * ``worst_gap_frac`` / ``worst_gap_from_zero_frac`` — the largest hole in
      the ladder, and where it sits. Position is what makes a gap benign or
      not: ~0 means it straddles silence (crossover distortion), ±0.5 means it
      is out at an extreme, where it costs almost nothing.
    * ``crossover_gap_frac`` — the gap spanning zero specifically, the one a
      listener hears as grit on quiet passages.
    """
    if span <= 0:
        return {
            "ladder_bits": 0.0,
            "ladder_rms_err_frac": 0.0,
            "ladder_max_err_frac": 0.0,
            "worst_gap_frac": 0.0,
            "worst_gap_from_zero_frac": 0.0,
            "crossover_gap_frac": 0.0,
        }
    resid = achieved - targets
    rms = float(np.sqrt(np.mean(resid**2)))
    srt = np.unique(achieved)
    gaps = np.diff(srt)
    wi = int(np.argmax(gaps)) if gaps.size else 0
    mid = float(srt[wi] + srt[wi + 1]) / 2 if gaps.size else 0.0
    below = float(srt[srt <= 0].max()) if np.any(srt <= 0) else 0.0
    above = float(srt[srt >= 0].min()) if np.any(srt >= 0) else 0.0
    return {
        # A perfect 256-step ladder is 8 bits; rms == 0 means every target was
        # hit exactly, which only happens on synthetic input.
        "ladder_bits": round(float(np.log2(span / (rms * np.sqrt(12)))), 2) if rms else 8.0,
        "ladder_rms_err_frac": round(rms / span, 5),
        "ladder_max_err_frac": round(float(np.max(np.abs(resid))) / span, 5),
        "worst_gap_frac": round(float(gaps[wi]) / span, 4) if gaps.size else 0.0,
        "worst_gap_from_zero_frac": round(mid / span, 3) if gaps.size else 0.0,
        "crossover_gap_frac": round((above - below) / span, 4),
    }


def is_level_drift(diag: dict[str, Any]) -> bool:
    """Whether an unsteady ring's disagreement is a moving level rather than
    laps that genuinely differ — the :data:`PASS_RESIDUAL_DRIFT_RATIO`
    discriminator over :func:`_pass_gain_decomposition`'s diagnostics. A spread
    of zero is not drift: there is no disagreement to classify."""
    spread = float(diag.get("pass_spread_p95_frac", 0.0))
    resid = float(diag.get("pass_residual_frac", 0.0))
    return bool(spread) and resid <= PASS_RESIDUAL_DRIFT_RATIO * spread
