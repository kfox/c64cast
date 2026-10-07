"""The host-side rate controllers that pace NMI-driven $D418 DAC audio.

Pure functions and their tuning constants only: the host-DMA pace servo
(servo_period, servo_hold_period), the shared clamped PI step (pi_step) the
REU mic lead servo also runs on, the stall re-anchor helpers (stall_reanchor,
stall_lapped), and the adaptive NMI-rate loop's latch step (nmi_rate_step).
Nothing here touches hardware or holds state — the stateful wiring lives in
audio_rate.RateServo and audio.AudioStreamer, and the ring geometry the
controllers measure against lives in audio_handlers.

See docs/architecture/audio.md#audio_servopy--the-host-side-rate-controllers.
"""

from __future__ import annotations

import math

from c64cast.audio.audio_handlers import CHUNK_SIZE, RING_BUFFER_ADDR, RING_BUFFER_SIZE

# Host-DMA pacing servo (closed-loop W→R rate match). The worker paces ring
# writes strictly to wall-clock, so W advances at exactly sample_rate B/s, while
# R loses ~4% of its ticks to video DMA bus-halts (HW-measured ~7690 B/s against
# an 8000 B/s producer) — W laps the 8 KB ring every ~26 s. Since W is paced by
# time.sleep, the loop closes with zero C64 writes: the worker reads R once per
# chunk and a PI controller stretches or shrinks the per-chunk pace so the ring
# gap locks near half a ring. See scripts/diags/hostdma_drift_probe.py.
HOST_DMA_SERVO_TARGET_GAP = RING_BUFFER_SIZE // 2  # 4096 B (half ring)
# HW-empirical gains. The drift to cancel is ~310 B/s, i.e. a steady period
# stretch of ~+5 ms/chunk. KP = 5e-6 s/byte makes a 1000-byte phase error add
# +5 ms (recovers in ~1-2 s); KI, an order below, nulls the fixed offset
# proportional control alone would leave.
HOST_DMA_SERVO_KP = 5e-6  # s/byte            (HW-TUNABLE)
HOST_DMA_SERVO_KI = 5e-7  # s/(byte*chunk)    (HW-TUNABLE)
HOST_DMA_SERVO_INTEG_CLAMP = 0.5  # max |ki*integ|, frac of chunk_period
HOST_DMA_SERVO_PERIOD_MIN_FRAC = 0.5
HOST_DMA_SERVO_PERIOD_MAX_FRAC = 1.5
# The R read sits inside the paced loop, after the chunk's drip writes, which
# take about half the chunk period; a read slower than the rest of the period
# makes the worker late. Such a reading is not used, and the servo stops
# reading for a holdoff (holding its integral correction, servo_hold_period),
# so a slow server is not charged once per chunk. The
# holdoff doubles on each consecutive slow read, up to the max, and resets on
# a prompt one.
HOST_DMA_SERVO_READ_BUDGET_FRAC = 0.5  # of chunk_period
HOST_DMA_SERVO_READ_HOLDOFF_MIN_S = 1.0
HOST_DMA_SERVO_READ_HOLDOFF_MAX_S = 8.0
# The stall re-anchor's own R-read budget, as a fraction of the lead it puts
# W ahead of R in seconds. R moves on while the read is in flight, so the
# anchor is only ahead of the live R if the read beats the lead, and the
# quarter kept back (≈85 ms at 12 kHz) covers the NEUTRAL stomp before W's
# first write. The servo's
# per-chunk budget is far tighter (it is a pacing deadline, not a safety
# bound), and borrowing it threw away readings this one can safely use.
STALL_REANCHOR_READ_BUDGET_FRAC = 0.75  # of the re-anchor lead, in seconds
# A W still ahead of R by less than this past R's travel during the read
# counts as lapped: the refill's first write has to land before R gets there,
# and a quarter chunk is ≈21 ms at 12 kHz, several DMA writes. A whole chunk
# NEUTRAL-filled unplayed audio (656 B in one virtual-clock case) without
# saving a single lap-old replay over a 48-case sweep.
STALL_INSIDE_LEAD_SLACK = CHUNK_SIZE // 4  # bytes

# Adaptive NMI-rate compensation: a slow outer loop that shrinks the CIA #2
# Timer A latch until the *measured* R rate lands back at sample_rate, since the
# gap servo above otherwise locks playback to the bus-halt-throttled consumer.
# It servos on dR/dt, not the gap — the gap servo nulls the gap, so it carries
# no rate signal and the two loops would fight. Clamped to the handler cycle
# budget (c64.NMI_SAFE_MIN_PERIOD_CYCLES). Off by default; see
# docs/architecture/audio.md#host-dma-pitch-compensation--why-two-of-the-three-knobs-default-off.
#
# The deadband MUST stay above half a latch quantum. The latch is an integer and
# one step moves the rate by 1/latch (~0.8% at 8 kHz, ~1.35% at the ceiling latch
# 74); a target between two grid rates lies within half a step of one of them,
# so a deadband wider than half the widest step always leaves a latch to park
# on. Narrower, the loop limit-cycles ±1 step, an audible ~1% pitch wobble.
# 0.013 is about one full step, which leaves headroom for estimator noise.
# The EMA alpha sets the estimator time constant (~chunk_period/alpha ≈ 2.1 s at
# 12 kHz / 1024-byte chunks) — long enough to reject torn-16-bit-read noise,
# short enough to re-acquire after a scene cut. The coarse zone converges a cold
# start in ~2-3 s instead of ~9 s; the fine zone moves ±1 so steady-state pitch
# steps are inaudible.
NMI_RATE_LOOP_DEADBAND_FRAC = 0.013  # > half the widest latch step; avoids limit cycle
NMI_RATE_LOOP_COARSE_ZONE_FRAC = 0.03  # above this error, take a proportional step
NMI_RATE_LOOP_MAX_COARSE_STEP = 4  # cap acquisition step (latch units)
NMI_RATE_LOOP_EMA_ALPHA = 0.04  # per-chunk EMA weight for the R-rate estimate (fine)
# Acquisition phase: a more responsive EMA and a short decision cadence walk the
# latch to convergence in ~0.5 s, so the start-of-playback pitch glide is brief
# instead of a ~3 s audible rise. The first decision needing no change flips to
# the fine loop above.
NMI_RATE_LOOP_ACQUIRE_ALPHA = 0.4  # responsive EMA during acquisition
NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS = 2  # decide every ~2 chunks while acquiring
# Warm-up gate: hold the latch at the near-converged seed and suppress decisions
# for this long after a consumer start or a large playback disturbance, while
# still feeding R into the EMA. The first R samples after a start/seek are
# unrepresentative — post-seek decode catch-up and the playlist's frame-drop snap
# mean R reads high until the steady-state VIC/DMA tick loss arrives (~3 s on HW:
# R ≈ 11.5k → 10.1k). Re-armed by AudioStreamer.note_playback_disturbance().
NMI_RATE_LOOP_WARMUP_S = 3.0
# Per-mode-class seed for the loop's starting latch, so playback begins near the
# converged rate. Bitmap modes lose ~10% of NMI ticks to the REU bank-swap +
# badline DMA and converge near the ceiling; char/light modes near nominal.
# Refined per mode by NmiTimer.learned_latch as scenes converge.
NMI_BITMAP_SEED_MODES = frozenset({"hires", "mhires"})


def servo_period(
    gap: int,
    integ: float,
    *,
    chunk_period: float,
    target_gap: int = HOST_DMA_SERVO_TARGET_GAP,
    kp: float = HOST_DMA_SERVO_KP,
    ki: float = HOST_DMA_SERVO_KI,
) -> tuple[float, float]:
    """PI controller on the host-DMA worker's per-chunk pace period.

    ``gap`` = (write_addr - R) % RING_BUFFER_SIZE — how far the write head W
    leads the NMI read pointer R. ``integ`` is the integrator state carried
    across chunks. Error ``e = gap - target_gap``; positive e means W is too far
    ahead, so we *lengthen* the period to slow W back toward target_gap. Returns
    ``(period_eff, new_integ)``.

    Proportional control alone turns the unbounded open-loop drift into a bounded
    constant phase offset (no lap); the integral term drives that residual offset
    to zero so the gap parks at target_gap. Pure (no I/O, no clock) so the
    control math is unit-testable without hardware.
    """
    # Anti-windup: bound the integral's *contribution* to ±INTEG_CLAMP·period.
    integ_limit = HOST_DMA_SERVO_INTEG_CLAMP * chunk_period / ki if ki > 0 else math.inf
    correction, integ = pi_step(
        gap - target_gap,
        integ,
        kp=kp,
        ki=ki,
        integ_min=-integ_limit,
        integ_max=integ_limit,
        out_min=(HOST_DMA_SERVO_PERIOD_MIN_FRAC - 1.0) * chunk_period,
        out_max=(HOST_DMA_SERVO_PERIOD_MAX_FRAC - 1.0) * chunk_period,
    )
    return chunk_period + correction, integ


def stall_reanchor(r_addr: int, chunk: int, lead: int = HOST_DMA_SERVO_TARGET_GAP) -> int:
    """Where the host-DMA write head restarts after a stall: the first
    chunk-grid address at least ``lead`` bytes ahead of R. The grid matters —
    every ring write is a whole chunk at a chunk-aligned address, which is what
    keeps a write from straddling ``RING_BUFFER_END`` (see the worker)."""
    ahead = r_addr - RING_BUFFER_ADDR + lead
    return RING_BUFFER_ADDR + (-(-ahead // chunk) * chunk) % RING_BUFFER_SIZE


def stall_lapped(r_addr: int, w_head: int, behind: int, slack: int) -> bool:
    """Whether R has reached the write head ``w_head`` (the end of what has
    landed) during a stall the worker came back ``behind`` bytes of
    consumption late; R is read after the stall.

    The ring gap alone cannot say: R known modulo the ring reads the same a
    few bytes short of W as a lap and a few bytes past it. The stall's length
    settles it. If W is still ahead, the gap is the lead W had less what R
    ate meanwhile, so gap + behind is that lead, under a ring. If R passed W
    by x, the gap is a ring less x and gap + behind is a ring plus the old
    lead, over one. The margin either side is the old lead's distance from a
    ring (≈4 KiB at the target gap, less as an open-loop lead grows) and the
    old lead itself, less what R moved during the read.

    A gap under ``slack`` counts as lapped too: R kept moving while it was
    read and while the caller acts on it, so a W only that far ahead may
    already be behind it. The caller passes R's travel over the read plus
    ``STALL_INSIDE_LEAD_SLACK``."""
    gap = (w_head - r_addr) % RING_BUFFER_SIZE
    return gap < slack or gap + behind >= RING_BUFFER_SIZE


def servo_hold_period(integ: float, *, chunk_period: float, ki: float = HOST_DMA_SERVO_KI) -> float:
    """The pace period with no gap reading to act on: only the integral term,
    which carries the standing rate correction (the consumer's bus-halt
    deficit), held where it was. Dropping to the bare ``chunk_period`` instead
    would hand that drift back, and the ring laps in about 26 s of it."""
    correction = max(
        (HOST_DMA_SERVO_PERIOD_MIN_FRAC - 1.0) * chunk_period,
        min((HOST_DMA_SERVO_PERIOD_MAX_FRAC - 1.0) * chunk_period, ki * integ),
    )
    return chunk_period + correction


def pi_step(
    error: float,
    integ: float,
    *,
    kp: float,
    ki: float,
    integ_min: float,
    integ_max: float,
    out_min: float,
    out_max: float,
) -> tuple[float, float]:
    """One step of a clamped PI controller: ``(kp·e + ki·integ, new_integ)``.

    ``integ`` accumulates ``error`` once per step and is held to
    ``[integ_min, integ_max]`` (anti-windup); the output is clamped to
    ``[out_min, out_max]``. Bounds that put ``ki·integ`` past the output clamp
    let the integrator wind up where the output cannot follow, so a caller with
    an asymmetric output range passes asymmetric integrator bounds. The host-DMA
    pace servo (``servo_period``) and the REU mic lead servo
    (``mic_lead.mic_lead_correction``) both run on it."""
    integ = max(integ_min, min(integ_max, integ + error))
    return max(out_min, min(out_max, kp * error + ki * integ)), integ


def nmi_rate_step(
    r_rate_ema: float,
    latch: int,
    *,
    nominal_latch: int,
    ceiling_latch: int,
    target_rate: float,
    deadband_frac: float = NMI_RATE_LOOP_DEADBAND_FRAC,
    coarse_zone_frac: float = NMI_RATE_LOOP_COARSE_ZONE_FRAC,
    max_coarse_step: int = NMI_RATE_LOOP_MAX_COARSE_STEP,
) -> int:
    """One decision of the adaptive NMI-rate loop: nudge ``latch`` so the measured
    consumer rate ``r_rate_ema`` moves toward ``target_rate`` (= sample_rate).

    Rate and latch are INVERSE (NMI period = latch+1 cycles), so R too slow
    (positive error) ⇒ DECREASE latch (faster NMI). The clamp range is
    ``[ceiling_latch, nominal_latch]``: ``nominal_latch`` is the rate floor (the
    latch for sample_rate, reached when there are no bus halts) and
    ``ceiling_latch`` is the fastest safe latch (handler cycle budget). The loop
    can therefore only SPEED UP from nominal toward the ceiling to overcome
    halt-induced tick loss; it can never push past the overrun guard.

    Deadband (> half a latch quantum) parks the integer latch instead of
    limit-cycling. Outside ``coarse_zone_frac`` a proportional step (capped)
    acquires fast; inside it moves ±1 so steady-state pitch steps are inaudible.
    Pure (no I/O) for unit testing — mirrors ``servo_period``."""
    if r_rate_ema <= 0 or target_rate <= 0:
        return latch
    err_frac = (target_rate - r_rate_ema) / target_rate
    if abs(err_frac) <= deadband_frac:
        return latch
    if abs(err_frac) > coarse_zone_frac:
        step = min(max_coarse_step, max(1, round(abs(err_frac) * (latch + 1))))
    else:
        step = 1
    # positive err (consumer too slow) → smaller latch (faster NMI)
    new_latch = latch - step if err_frac > 0 else latch + step
    return max(ceiling_latch, min(nominal_latch, new_latch))
