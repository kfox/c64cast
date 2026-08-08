"""NMI consumer rate control for the $D418 DAC streamer.

Two collaborators split out of ``AudioStreamer`` (2026-08), each holding a
back-reference to its streamer (the same pattern as
``playlist_support``/``video_transport``); bodies moved verbatim, log lines
and control math unchanged:

* ``NmiTimer`` — CIA #2 Timer A ownership: the latch math (nominal /
  ceiling / per-mode seed / pitch-compensated), the verified arm sequence,
  and the in-session per-mode learned-latch cache the adaptive loop seeds
  from.
* ``RateServo`` — the worker-thread closed loops: the per-chunk PI pace
  servo on the ring gap, the R-rate observer + adaptive NMI-rate outer
  loop that steers the timer, the consumer-stall watchdog, and the
  gap/rate telemetry the health line and stop() summary read.

Threading contract is unchanged from the in-streamer days: every RateServo
method runs on the audio worker thread (its fields need no lock), except
``note_disturbance`` (a single monotonic write, safe from any thread).
``NmiTimer.start`` runs on the worker; ``AudioStreamer.set_nmi_latch_for_
mode`` mutates timer/servo fields from the playlist thread exactly as it
always did.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from c64cast.hw.c64 import (
    CIA2,
    CLOCK_NTSC,
    CLOCK_PAL,
    NMI_SAFE_MIN_PERIOD_CYCLES,
    VECTORS,
)

from .audio_handlers import (
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    NMI_ARM_MAX_ATTEMPTS,
    NMI_ARM_VERIFY_DELAY_S,
    NMI_BITMAP_SEED_MODES,
    NMI_RATE_LOOP_ACQUIRE_ALPHA,
    NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS,
    NMI_RATE_LOOP_EMA_ALPHA,
    NMI_RATE_LOOP_WARMUP_S,
    NMI_ROUTINE_ADDR,
    NMI_STALL_WARN_CHUNKS,
    RING_BUFFER_SIZE,
    nmi_rate_step,
    servo_period,
)

if TYPE_CHECKING:
    from .audio import AudioStreamer

log = logging.getLogger(__name__)


class NmiTimer:
    """CIA #2 Timer A ownership for the NMI DAC consumer."""

    def __init__(self, streamer: AudioStreamer) -> None:
        self._st = streamer
        # The latch the NMI consumer runs at (set by start()); the REU pump's
        # nominal CIA #1 latch derives from it so the producer/consumer
        # period ratio stays exact.
        self.latch = 0
        # Host-DMA-servo pitch compensation: a sticky per-display-mode
        # playback-rate multiplier (>1.0 = faster). set_nmi_latch_for_mode
        # updates it, and start() applies it when the timer first arms — so a
        # multiplier set at scene setup (before the worker prebuffers and
        # starts the timer) survives the timer start instead of being
        # clobbered back to nominal. `started` gates whether a mid-stream
        # update writes immediately.
        self.pitch_multiplier = 1.0
        self.started = False
        # How many arms the last bring-up needed (1 = clean, or an
        # unverifiable backend).
        self.arm_attempts = 0
        # Current display mode (set by set_nmi_latch_for_mode) + an
        # in-session cache of each mode's converged latch. The adaptive loop
        # SEEDS the starting latch from these so playback begins at ~the
        # right rate (no start-of-playback pitch glide) and re-converges fast
        # on a mode change. The cache persists across scenes/loops
        # (deliberately NOT reset by reset_after_stop); per-process only.
        self.mode: str | None = None
        self.learned_latch: dict[str, int] = {}

    def nominal_latch(self) -> int:
        """CIA #2 Timer A latch for the NMI DAC consumer at sample_rate.

        Timer A counts N→0 inclusive = N+1 PHI2 ticks per fire, so the NMI
        period is (latch+1) cycles. Pick the integer latch whose (latch+1)
        period brings the consumer rate closest to sample_rate. NTSC@8kHz:
        latch=127 (7990 Hz, -0.12%); PAL@8kHz: latch=122 (8010 Hz, +0.13%).
        The REU pump's CIA #1 latch and the servo's feed-forward both derive
        from this so the producer/consumer ratio stays exact.

        The rate that latch actually yields is `effective_rate` — read that,
        not `sample_rate`, whenever the number means real time.
        """
        clock = CLOCK_NTSC if self._st.system == "NTSC" else CLOCK_PAL
        return max(1, round(clock / self._st.sample_rate) - 1)

    @property
    def effective_rate(self) -> float:
        """The rate the C64 NMI consumer *actually* runs at, in Hz — see
        ``AudioStreamer.effective_rate`` (the public read surface) for the
        full timebase rationale."""
        if not self._st.sample_rate:
            # Callers treat a falsy rate as "no audio clock" (see
            # position_seconds); nominal_latch would divide by zero.
            return 0.0
        clock = CLOCK_NTSC if self._st.system == "NTSC" else CLOCK_PAL
        return clock / (self.nominal_latch() + 1)

    def ceiling_latch(self) -> int:
        """Smallest (fastest) CIA #2 Timer A latch the adaptive loop may use: the
        latch whose NMI period equals the safe handler budget
        (c64.NMI_SAFE_MIN_PERIOD_CYCLES). period = latch+1, so latch = budget-1.
        Bounds how far the loop can speed the NMI to overcome bus-halt tick loss
        without overrunning the handler. System-independent (a cycle count)."""
        return max(1, NMI_SAFE_MIN_PERIOD_CYCLES - 1)

    def seed_latch_for_mode(self, mode: str | None) -> int:
        """Starting CIA #2 latch for the adaptive loop, chosen so playback begins
        near the converged rate (minimal start glide). Uses the in-session learned
        value for `mode` if known; else a per-mode-class default — bitmap modes
        (heavy bus-halt loss) seed at the ceiling, char/light/unknown modes at
        nominal. Clamped to the safe [ceiling, nominal] range."""
        nominal = self.nominal_latch()
        ceiling = self.ceiling_latch()
        if mode is not None and mode in self.learned_latch:
            seed = self.learned_latch[mode]
        elif mode in NMI_BITMAP_SEED_MODES:
            seed = ceiling
        else:
            seed = nominal
        return max(ceiling, min(nominal, seed))

    def compensated_latch(self) -> int:
        """The CIA #2 Timer A latch for the current pitch multiplier.

        Rate and latch are inverse — NMI period = (latch+1) cycles — so a >1.0
        (faster) multiplier shortens the nominal period: period =
        round((nominal+1) / mult), latch = period − 1. Multiplier 1.0 → nominal.
        """
        nominal_latch = self.nominal_latch()
        adjusted_period = max(2, round((nominal_latch + 1) / self.pitch_multiplier))
        return max(1, adjusted_period - 1)

    def write_latch(self, latch: int) -> None:
        """Record + write a new CIA #2 Timer A latch (the one live retune
        primitive both the adaptive loop and the static retune use)."""
        self.latch = latch
        self._st.api.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)

    def arm_once(self, latch: int) -> None:
        """One full arm of the NMI audio consumer, idempotent so a retry is just
        another call.

        The `$0318` vector is re-landed here, not only in _upload_nmi_and_buffers:
        if *that* write is the one the transport dropped, the KERNAL NMI handler
        is still installed, and its `#$7F` → `$DD0D` kills CIA #2 interrupts —
        indistinguishable from a dropped CIA write.

        The `$DD0D` read is how the latched ICR flags get cleared (a write can't),
        deasserting the CIA's interrupt line so the next Timer A underflow is a
        clean 0→1 transition. Best-effort: a backend that can't read still arms.
        """
        api = self._st.api
        api.write_regs(
            f"{VECTORS.NMI:04X}", NMI_ROUTINE_ADDR & 0xFF, (NMI_ROUTINE_ADDR >> 8) & 0xFF
        )
        api.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
        try:
            api.read_memory(CIA2.ICR, 1)
        except Exception as e:
            log.debug("ICR flag clear read failed: %s", e)
        # Arm + start timer A, set NMI source.
        api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)

    def start(self, *, adaptive: bool) -> None:
        """Arm the NMI timer with verification (called from the worker after
        the prebuffer fills).

        Adaptive mode: arm at the per-mode seed (learned value or class
        default) so playback starts near the converged rate — the loop trims
        from there instead of gliding up from nominal. Static mode: apply the
        pitch multiplier chosen for this scene (the timer arms from the worker
        after prebuffer, i.e. AFTER set_nmi_latch_for_mode, so honoring it
        here is what makes the static compensation stick instead of resetting
        to nominal)."""
        latch = self.seed_latch_for_mode(self.mode) if adaptive else self.compensated_latch()
        self.latch = latch
        self.started = True
        before = self._st.read_consumer_ptr()
        if before is None:
            # No R to check against, so a retry would be indistinguishable from
            # arming five times for nothing. Fire once and accept the risk.
            self.arm_attempts = 1
            self.arm_once(latch)
            return
        for attempt in range(1, NMI_ARM_MAX_ATTEMPTS + 1):
            self.arm_attempts = attempt
            self.arm_once(latch)
            time.sleep(NMI_ARM_VERIFY_DELAY_S)
            after = self._st.read_consumer_ptr()
            if after is None or after != before:
                # R advanced (or went unreadable, which is not evidence of a dead
                # consumer) — the arm took.
                if attempt > 1:
                    log.warning(
                        "audio: NMI arm took %d attempts (a CIA write was dropped)", attempt
                    )
                else:
                    log.debug("audio: NMI arm verified first attempt (R was $%04X)", before)
                return
        log.warning(
            "audio: NMI consumer never started after %d arm attempts — audio will be "
            "silent this session (R frozen at $%04X). The CIA #2 / NMI-vector writes "
            "are not reaching the machine.",
            NMI_ARM_MAX_ATTEMPTS,
            before,
        )

    def reset_after_stop(self) -> None:
        """Clear pitch-comp + arm state so the next scene's bring-up re-arms
        from nominal (a scene with no display_mode never calls
        set_nmi_latch_for_mode, so a stale multiplier must not leak across
        scenes). The per-mode learned-latch cache deliberately survives."""
        self.started = False
        self.pitch_multiplier = 1.0
        self.arm_attempts = 0


class RateServo:
    """The worker-thread closed loops: gap servo, R-rate observer, adaptive
    NMI-rate steering, stall watchdog, and their telemetry."""

    def __init__(self, streamer: AudioStreamer, timer: NmiTimer) -> None:
        self._st = streamer
        self._timer = timer
        # PI integrator state for the host-DMA servo (worker-thread-only, so no
        # lock needed). Reset to 0 each time the NMI consumer starts.
        self.integ = 0.0
        # Adaptive NMI-rate loop state (worker-thread-only). r_rate_ema = -1.0
        # is the unseeded sentinel; all reset with `integ` at consumer start.
        self.r_rate_ema = -1.0
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.loop_chunk_count = 0
        self.loop_acquiring = True
        # Warm-up gate deadline (monotonic). While now < this, the loop measures R
        # into the EMA but holds the latch (see NMI_RATE_LOOP_WARMUP_S). 0.0 = open
        # (no warm-up pending), so direct update_rate_loop calls act at once.
        # Armed at consumer start and re-armed by note_disturbance().
        self.warmup_until = 0.0
        # Host-DMA servo gap telemetry (write head's lead over R, in bytes),
        # for non-ears verification via the drift probe / stop() summary. -1 =
        # no servo sample taken yet this session.
        self.gap_min = -1
        self.gap_max = -1
        self.gap_last = -1
        # Stall watchdog: consecutive-identical-R count so the mid-session
        # stall warning fires once.
        self.last_r_reading = -1
        self.r_stall_chunks = 0
        self.stall_warned = False
        # Health-line window state the streamer's _maybe_log_health reads and
        # resets: the gap's excursion within the window, and the instantaneous
        # consumer-rate excursion (the EMA alone smooths away exactly the
        # wander worth seeing).
        self.health_gap_min = -1
        self.health_gap_max = -1
        self.r_rate_min = -1.0
        self.r_rate_max = -1.0

    def next_pace_increment(self, write_addr: int, chunk_period: float) -> float:
        """Per-chunk pace increment for the prebuffered worker.

        Open-loop (host_dma_servo off, or a failed/insane R read) returns the
        bare ``chunk_period`` — the original strict wall-clock schedule. With the
        servo on, reads the NMI read pointer R over REST, computes the ring gap
        ``(write_addr - R) % RING_BUFFER_SIZE`` (write_addr is the live W head —
        already advanced past the byte just written), and runs the PI controller
        (``servo_period``) so W's pace tracks R and the gap locks near half a
        ring instead of lapping. A flaky read degrades to open-loop for that one
        chunk; it never crashes or freezes the schedule. The increment is added
        to the *absolute* ``next_write_time`` by the caller, so REST read latency
        only shortens the next sleep — it does not snap the schedule forward.

        Also the only place a consumer that dies *mid*-session becomes visible —
        see ``note_r_reading``.
        """
        st = self._st
        if not st.host_dma_servo:
            return chunk_period
        r_addr = st.read_consumer_ptr()
        if r_addr is None:
            return chunk_period
        self.note_r_reading(r_addr)
        gap = (write_addr - r_addr) % RING_BUFFER_SIZE
        self.gap_last = gap
        self.gap_min = gap if self.gap_min < 0 else min(self.gap_min, gap)
        self.gap_max = max(self.gap_max, gap)
        self.health_gap_min = gap if self.health_gap_min < 0 else min(self.health_gap_min, gap)
        self.health_gap_max = max(self.health_gap_max, gap)
        # Slow outer loop: track R's *rate* and retune the NMI latch so the
        # consumer lands at sample_rate (correct speed/pitch). Reuses the r_addr
        # already read above — no extra REST traffic. Reads the rate, not the gap
        # (the gap servo below nulls the gap, so it carries no rate signal).
        # The rate estimate is taken either way — it is the only view of
        # content-dependent tick loss, and it costs nothing on top of the read
        # the gap servo just did. Only the latch *steering* is opt-in.
        if st.nmi_rate_adaptive:
            self.update_rate_loop(r_addr)
        else:
            self.observe_r_rate(r_addr)
        period, self.integ = servo_period(gap, self.integ, chunk_period=chunk_period)
        return period

    def note_r_reading(self, r_addr: int) -> None:
        """Watch for an NMI consumer that stopped after a verified start.

        A consumer killed mid-session (a stray `#$7F` to `$DD0D`, a reset behind
        our back) otherwise presents as unexplained silence plus the fast
        playback the servo produces while chasing a dead reader — the servo has
        this reading in hand either way, so saying so costs nothing. Warns once
        per session; the pacing behavior is untouched.
        """
        if r_addr == self.last_r_reading:
            self.r_stall_chunks += 1
        else:
            self.last_r_reading = r_addr
            self.r_stall_chunks = 0
        if self.r_stall_chunks >= NMI_STALL_WARN_CHUNKS and not self.stall_warned:
            self.stall_warned = True
            log.warning(
                "audio: NMI consumer stalled — R has not moved from $%04X for %d chunks. "
                "Audio is silent and playback pace is unreliable from here.",
                r_addr,
                self.r_stall_chunks,
            )

    def observe_r_rate(self, r_addr: int) -> None:
        """Track the NMI consumer's byte rate dR/dt, from the R the gap servo
        already read. Observation only — nothing here steers anything.

        Split out of ``update_rate_loop`` because that loop is off by
        default (steering on R is a closed dead end: R is a biased estimator
        under bus load, measured biased in *both* directions depending on read
        method). Gating the measurement on the disabled controller meant the one
        quantity that shows content-dependent tick loss read zero in every log,
        which is a diagnostic hole rather than a safety property — the guardrail
        is against controlling on R, not against looking at it.

        The instantaneous per-chunk rate is kept alongside the EMA: the EMA says
        where the consumer sits, the spread between successive instantaneous
        values says how much it is *moving*, and a consumer whose rate wanders
        drags playback pitch with it through the gap servo, which faithfully
        follows R by design.
        """
        alpha = NMI_RATE_LOOP_ACQUIRE_ALPHA if self.loop_acquiring else NMI_RATE_LOOP_EMA_ALPHA
        now = time.monotonic()
        if self.last_r_addr >= 0 and self.last_r_time > 0.0:
            dt = now - self.last_r_time
            dr = (r_addr - self.last_r_addr) % RING_BUFFER_SIZE
            # Discard a torn/backward read (half-ring jump = a read tear mid
            # self-modify, not real advance) — same guard as hostdma_drift_probe.
            if dt > 0 and dr < RING_BUFFER_SIZE // 2:
                inst = dr / dt
                if self.r_rate_ema < 0:
                    self.r_rate_ema = inst  # seed (no ramp-from-zero)
                else:
                    self.r_rate_ema += alpha * (inst - self.r_rate_ema)
                self.r_rate_min = inst if self.r_rate_min < 0 else min(self.r_rate_min, inst)
                self.r_rate_max = max(self.r_rate_max, inst)
        self.last_r_addr = r_addr
        self.last_r_time = now

    def update_rate_loop(self, r_addr: int) -> None:
        """Estimate the NMI consumer's byte rate (dR/dt) and step the CIA #2
        Timer A latch toward making it equal sample_rate — fast at first
        (acquisition, ~0.5 s, so the start glide is brief), then ~once per second.

        Called per chunk from next_pace_increment with the already-read R
        address, so it adds no REST traffic and only runs on the host-DMA path
        (the REU pump never starts the worker; open-loop returns before this).
        The rate estimate itself comes from observe_r_rate, which runs whether
        or not this loop does. The actual latch move is the pure nmi_rate_step
        (clamped to the handler budget)."""
        st = self._st
        timer = self._timer
        self.observe_r_rate(r_addr)
        acquiring = self.loop_acquiring
        # Warm-up gate: during the post-start / post-disturbance settle window the
        # EMA keeps warming (in observe_r_rate) but the latch is held at the seed
        # — the seed is already near-converged, so this plays at ~the right rate
        # instead of chasing the unrepresentative spin-up R and gliding back. The
        # chunk counter is not advanced, so the decide cadence resumes cleanly.
        if time.monotonic() < self.warmup_until:
            return

        self.loop_chunk_count += 1
        decide_every = (
            NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS
            if acquiring
            else max(1, round(st.sample_rate / st.chunk_size))
        )
        if self.loop_chunk_count < decide_every or self.r_rate_ema < 0:
            return
        self.loop_chunk_count = 0
        if not timer.started:
            return
        new_latch = nmi_rate_step(
            self.r_rate_ema,
            timer.latch,
            nominal_latch=timer.nominal_latch(),
            ceiling_latch=timer.ceiling_latch(),
            # The loop drives the measured consumer rate R toward this. R
            # physically runs on the latch grid, so targeting the *request*
            # would walk the latch off nominal by the quantization error.
            target_rate=timer.effective_rate,
        )
        if new_latch == timer.latch:
            # No change → within deadband or clamped at the ceiling: converged.
            # Leave fast acquisition for the gentle fine loop (slow ±1 steps), and
            # remember this mode's converged latch so the next scene/loop in this
            # mode seeds dead-on (no start glide).
            self.loop_acquiring = False
            if timer.mode is not None:
                timer.learned_latch[timer.mode] = timer.latch
            return
        log.debug(
            "[audio] adaptive NMI rate%s: R≈%.0f / %d Hz target, latch %d → %d",
            " (acquire)" if acquiring else "",
            self.r_rate_ema,
            st.sample_rate,
            timer.latch,
            new_latch,
        )
        timer.write_latch(new_latch)

    def note_disturbance(self) -> None:
        """Re-arm the adaptive NMI-rate loop's warm-up gate after a large
        playback disturbance — see ``AudioStreamer.note_playback_disturbance``
        (the public entry the playlist calls). Cheap + thread-safe: a single
        monotonic write."""
        self.warmup_until = time.monotonic() + NMI_RATE_LOOP_WARMUP_S

    def reset_for_consumer_start(self) -> None:
        """R only becomes meaningful once the NMI consumes; start the servo
        integrator + adaptive-rate loop clean and hold the rate loop at the
        seed until the start/seek transient settles (post-seek decode catch-up
        + the playlist's frame-drop snap), so it acquires from a steady R
        instead of chasing the spin-up reading. See NMI_RATE_LOOP_WARMUP_S."""
        self.integ = 0.0
        self.r_rate_ema = -1.0
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.health_gap_min = -1
        self.health_gap_max = -1
        self.loop_chunk_count = 0
        self.loop_acquiring = True
        self.warmup_until = time.monotonic() + NMI_RATE_LOOP_WARMUP_S

    def reset_after_stop(self) -> None:
        """Clear the watchdog + adaptive-rate state so the next consumer
        re-acquires from nominal rather than carrying a stale R-rate
        estimate."""
        self.last_r_reading = -1
        self.r_stall_chunks = 0
        self.stall_warned = False
        self.r_rate_ema = -1.0
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.loop_chunk_count = 0
        self.loop_acquiring = True
