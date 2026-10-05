"""NMI consumer rate control for the $D418 DAC streamer.

Two collaborators of ``AudioStreamer``, each holding a back-reference to it
(the same pattern as ``playlist_support``/``video_transport``):

* ``NmiTimer`` — CIA #2 Timer A ownership: the latch math (nominal /
  ceiling / per-mode seed / pitch-compensated), the verified arm sequence,
  and the in-session per-mode learned-latch cache the adaptive loop seeds
  from.
* ``RateServo`` — the worker-thread closed loops: the per-chunk PI pace
  servo on the ring gap, the R-rate observer + adaptive NMI-rate outer
  loop that steers the timer, the consumer-stall watchdog, and the
  gap/rate telemetry the health line and stop() summary read.

**Threading contract.** Every RateServo method runs on the audio worker
thread (its fields need no lock), except ``note_disturbance`` (a single
monotonic write, safe from any thread). ``NmiTimer.start`` runs on the
worker; ``AudioStreamer.set_nmi_latch_for_mode`` mutates timer/servo fields
from the playlist thread.

See docs/architecture/audio.md#audiopy--audiostreamer.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from c64cast._transport_log import quiet_transport
from c64cast.hw.c64 import (
    CIA2,
    CIA_TIMER_LATCH_MAX,
    NMI_SAFE_MIN_PERIOD_CYCLES,
    VECTORS,
    cpu_clock,
    nearest_latch,
)

from .audio_handlers import (
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    HOST_DMA_SERVO_READ_BUDGET_FRAC,
    HOST_DMA_SERVO_READ_HOLDOFF_MAX_S,
    HOST_DMA_SERVO_READ_HOLDOFF_MIN_S,
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
    RING_LEAD_EMA_ALPHA,
    nmi_rate_step,
    servo_hold_period,
    servo_period,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .audio import AudioStreamer

log = logging.getLogger(__name__)


class NmiTimer:
    """CIA #2 Timer A ownership for the NMI DAC consumer."""

    def __init__(self, streamer: AudioStreamer) -> None:
        self._st = streamer
        # Set by start(); the REU pump's nominal CIA #1 latch derives from
        # this, so the producer/consumer period ratio stays exact.
        self.latch = 0
        # The fastest (smallest) latch armed since the servo last took it —
        # see take_fastest_latch. 0 = none armed yet.
        self.fastest_latch = 0
        # Sticky per-display-mode playback-rate multiplier (>1.0 = faster),
        # applied by start(); `started` gates a mid-stream update.
        self.pitch_multiplier = 1.0
        self.started = False
        # 1 = clean, or an unverifiable backend.
        self.arm_attempts = 0
        # Per-mode converged latches the adaptive loop seeds from. In-session
        # and per-process; NOT cleared by reset_after_stop.
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

        Held to [ceiling_latch, CIA_TIMER_LATCH_MAX]: a rate past the handler budget
        arms at the ceiling and one too slow for 16 bits at the maximum, so
        `effective_rate` reports what is armed rather than what was asked. Load
        rejects both, but an unresolved "auto" is validated as NTSC and a PAL
        machine's ceiling sits lower; `start` warns when the clamp engages.
        """
        return self.clamp_latch(self.requested_latch())

    def requested_latch(self) -> int:
        """The nearest-grid latch for sample_rate, before any clamp."""
        return nearest_latch(self._st.sample_rate, self._st.system)

    def clamp_latch(self, latch: int) -> int:
        """`latch` held to what the handler budget and the 16-bit timer allow."""
        return max(self.ceiling_latch(), min(CIA_TIMER_LATCH_MAX, latch))

    @property
    def effective_rate(self) -> float:
        """The rate the C64 NMI consumer *actually* runs at, in Hz — see
        ``AudioStreamer.effective_rate`` (the public read surface) for the
        full timebase rationale."""
        if not self._st.sample_rate:
            # Callers read a falsy rate as "no audio clock" (position_seconds);
            # nominal_latch would divide by zero.
            return 0.0
        clock = cpu_clock(self._st.system)
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
        nominal. Clamped to the safe [ceiling, nominal] range, which is never
        empty because nominal_latch is itself held at or above the ceiling."""
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
        Clamped like `nominal_latch`, so no multiplier arms a period shorter
        than the handler budget — the bound the adaptive loop already had.
        """
        return self.clamp_latch(self.requested_compensated_latch())

    def requested_compensated_latch(self) -> int:
        """The latch the pitch multiplier asks for, before the clamp.

        A vanishing multiplier (1e-320 loads as positive) overflows the period
        to inf, which round() cannot convert; that period is past the 16-bit
        timer, so it is reported as the first latch beyond it."""
        if not self.pitch_multiplier > 0:
            raise ValueError(f"pitch multiplier must be positive, got {self.pitch_multiplier!r}")
        period = (self.nominal_latch() + 1) / self.pitch_multiplier
        if not math.isfinite(period):
            return CIA_TIMER_LATCH_MAX + 1
        return round(period) - 1

    def write_latch(self, latch: int) -> None:
        """Record + write a new CIA #2 Timer A latch — the one live retune
        primitive both the adaptive loop and the static retune use."""
        self.latch = latch
        self._note_armed(latch)
        self._st.api.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)

    def _note_armed(self, latch: int) -> None:
        self.fastest_latch = latch if self.fastest_latch <= 0 else min(self.fastest_latch, latch)

    def take_fastest_latch(self) -> int:
        """The fastest latch armed since the previous call, the one armed now
        included, and restart the tracking at the one armed now. A retune
        between two R readings (the adaptive loop, a mode change's seed or
        pitch multiplier) leaves the latch slower than the consumer ran for
        part of that interval; the servo's wrap bound has to use the fastest.
        0 when nothing has been armed."""
        fastest = self.fastest_latch
        self.fastest_latch = self.latch
        if fastest <= 0:
            return self.latch
        return min(fastest, self.latch) if self.latch > 0 else fastest

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
        to nominal).

        The backend hears of the consumer only once the arm has taken (or
        cannot be checked): a TeensyROM+ slices every write while one runs, and
        noting it at upload made the whole prebuffer go out in slices with no
        NMI there to spare."""
        requested = self.requested_latch()
        if requested != self.nominal_latch():
            log.warning(
                "audio: sample_rate %d Hz needs CIA #2 latch %d on %s, outside the "
                "%d..%d the NMI handler budget and the 16-bit timer allow — playing "
                "at %.0f Hz instead",
                self._st.sample_rate,
                requested,
                self._st.system,
                self.ceiling_latch(),
                CIA_TIMER_LATCH_MAX,
                self.effective_rate,
            )
        latch = self.seed_latch_for_mode(self.mode) if adaptive else self.compensated_latch()
        self.latch = latch
        self._note_armed(latch)
        self.started = True
        before = self._st.read_consumer_ptr()
        if before is None:
            # Unverifiable without R, so retrying would just arm N times blind.
            self.arm_attempts = 1
            self.arm_once(latch)
            self._st.api.note_nmi_consumer(True)
            return
        for attempt in range(1, NMI_ARM_MAX_ATTEMPTS + 1):
            self.arm_attempts = attempt
            self.arm_once(latch)
            time.sleep(NMI_ARM_VERIFY_DELAY_S)
            after = self._st.read_consumer_ptr()
            if after is None or after != before:
                # R advanced, or went unreadable — which is not evidence of a
                # dead consumer. Either way the arm took.
                if attempt > 1:
                    log.warning(
                        "audio: NMI arm took %d attempts (a CIA write was dropped)", attempt
                    )
                else:
                    log.debug("audio: NMI arm verified first attempt (R was $%04X)", before)
                self._st.api.note_nmi_consumer(True)
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
        # Host-DMA servo PI integrator; zeroed at each consumer start.
        self.integ = 0.0
        # Adaptive NMI-rate loop state; r_rate_ema = -1.0 is the unseeded
        # sentinel, and all of it resets with `integ` at consumer start.
        self.r_rate_ema = -1.0
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.loop_chunk_count = 0
        self.loop_acquiring = True
        # Warm-up gate deadline (monotonic); 0.0 = open, so a direct
        # update_rate_loop call acts at once.
        self.warmup_until = 0.0
        # Gap telemetry: the write head's lead over R in bytes, read by the
        # drift probe and the stop() summary. -1 = no sample yet this session.
        self.gap_min = -1
        self.gap_max = -1
        self.gap_last = -1
        # The unplayed bytes ahead of R, smoothed: what position_seconds()
        # subtracts to report what is heard. -1 = no consumer this run.
        self.ring_lead = -1.0
        self.last_r_reading = -1
        self.last_r_reading_since = 0.0  # monotonic, when R took that value
        self.r_stall_chunks = 0
        self.stall_warned = False
        # Slow-R-read holdoff: no read before `read_holdoff_until` (monotonic),
        # and the next slow read holds off for `read_holdoff_s` (0 = none yet).
        self.read_holdoff_until = 0.0
        self.read_holdoff_s = 0.0
        self.slow_reads = 0
        self.slow_read_warned = False
        # How long the read that armed the current backoff took.
        self.last_slow_read_s = 0.0
        # Per-window excursions, read and reset by _maybe_log_health.
        self.health_gap_min = -1
        self.health_gap_max = -1
        self.r_rate_min = -1.0
        self.r_rate_max = -1.0

    def next_pace_increment(self, write_addr: int, chunk_period: float) -> float:
        """Per-chunk pace increment for the prebuffered worker.

        Open-loop (host_dma_servo off) returns the bare ``chunk_period`` — the
        original strict wall-clock schedule. With the
        servo on, reads the NMI read pointer R over REST, computes the ring gap
        ``(write_addr - R) % RING_BUFFER_SIZE`` (write_addr is the live W head —
        already advanced past the byte just written), and runs the PI controller
        (``servo_period``) so W's pace tracks R and the gap locks near half a
        ring instead of lapping. A failed or out-of-ring read holds the integral
        correction for that one chunk (``servo_hold_period``), as a slow one
        does; it never crashes or freezes the schedule. The increment is added
        to the *absolute* ``next_write_time`` by the caller, so REST read latency
        only shortens the next sleep — it does not snap the schedule forward.

        A read slower than ``HOST_DMA_SERVO_READ_BUDGET_FRAC`` of the chunk
        period is not paced by (only the stall watchdog sees it), and the
        servo stops reading for a backoff, holding its integral correction
        (``servo_hold_period``) meanwhile. A slow server charged once per chunk
        otherwise makes the worker fall steadily behind the consumer, which
        then plays the ring's previous lap.

        Also the only place a consumer that dies *mid*-session becomes visible —
        see ``note_r_reading``.
        """
        st = self._st
        if not st.host_dma_servo:
            return chunk_period
        if self.reads_held_off():
            return servo_hold_period(self.integ, chunk_period=chunk_period)
        # The one read this class repeats — held out of `-vv` so a chunk-rate
        # transport record doesn't bury the requests an operator came for. The
        # arm verification and the pause stomp call `read_consumer_ptr` too,
        # and those are one-shot, so they stay visible.
        with quiet_transport():
            r_addr, prompt, _ = self._timed_read(HOST_DMA_SERVO_READ_BUDGET_FRAC * chunk_period)
        if r_addr is not None:
            # A late reading is too late to pace by but still says whether the
            # consumer is alive: a server that stays slow sends every reading
            # past the gap servo, and the watchdog would otherwise see none.
            self.note_r_reading(r_addr)
        if r_addr is None or not prompt:
            return servo_hold_period(self.integ, chunk_period=chunk_period)
        gap = (write_addr - r_addr) % RING_BUFFER_SIZE
        self.gap_last = gap
        self.gap_min = gap if self.gap_min < 0 else min(self.gap_min, gap)
        self.gap_max = max(self.gap_max, gap)
        self.health_gap_min = gap if self.health_gap_min < 0 else min(self.health_gap_min, gap)
        self.health_gap_max = max(self.health_gap_max, gap)
        if self.ring_lead >= 0:
            self.ring_lead += RING_LEAD_EMA_ALPHA * (gap - self.ring_lead)
        # Slow outer loop, on R's *rate*: the gap servo below nulls the gap,
        # so the gap carries no rate signal. Measured either way; only the
        # latch steering is opt-in.
        if st.nmi_rate_adaptive:
            self.update_rate_loop(r_addr)
        else:
            self.observe_r_rate(r_addr)
        period, self.integ = servo_period(gap, self.integ, chunk_period=chunk_period)
        return period

    def reads_held_off(self) -> bool:
        """True while a slow read's backoff is running: the servo does not read
        R, and the stall re-anchor reads it only as ``read_r_promptly`` says."""
        return time.monotonic() < self.read_holdoff_until

    def _timed_read(
        self, budget_s: float, current: Callable[[], bool] | None = None
    ) -> tuple[int | None, bool, float]:
        """Read R once against ``budget_s``; returns ``(r_addr, prompt,
        took)``. A read over budget arms the backoff and comes back with
        ``prompt`` False; a prompt one resets the backoff.

        ``current`` says whether the caller still owns this session. A read
        that returns after it stopped doing so comes back ``(None, False,
        took)`` and leaves the backoff, its counters and its warning alone:
        they belong to the next session by then."""
        started = time.monotonic()
        r_addr = self._st.read_consumer_ptr()
        took = time.monotonic() - started
        if current is not None and not current():
            return None, False, took
        if took > budget_s:
            self._hold_off_slow_read(took, budget_s)
            return r_addr, False, took
        # The stall re-anchor can read inside a backoff, so a prompt read ends
        # the running hold as well as restarting the doubling.
        self.read_holdoff_s = 0.0
        self.read_holdoff_until = 0.0
        return r_addr, True, took

    def read_r_promptly(
        self, chunk_period: float, budget_s: float, current: Callable[[], bool]
    ) -> int | None:
        """R for a one-off decision that acts on where R is *now* (the stall
        re-anchor), or None. ``budget_s`` is how stale that decision can
        stand R: R moves on while the read is in flight, by up to the read's
        whole duration, so a read slower than that comes back None. So does
        a call inside a backoff armed by a read already slower than
        ``budget_s``, without reading. A backoff armed by a read the servo
        could not pace by but that beats ``budget_s`` does not stop this one:
        it is a single read, and the reading is still good enough to act on.
        The read counts toward the servo's backoff like any other. With the
        servo off there is no pacing budget, and this read is the only one,
        so it is judged against ``budget_s`` alone.

        ``current`` is the caller's fence: a worker parked in this read can
        outlive stop() and the next start_*, and a read it gets back after
        ``current()`` turns False is None, without touching the backoff the
        next session reads by (see ``_timed_read``)."""
        if self.reads_held_off() and self.last_slow_read_s > budget_s:
            return None
        backoff_budget_s = (
            HOST_DMA_SERVO_READ_BUDGET_FRAC * chunk_period if self._st.host_dma_servo else budget_s
        )
        r_addr, _, took = self._timed_read(backoff_budget_s, current)
        return r_addr if took <= budget_s else None

    def _hold_off_slow_read(self, took: float, budget_s: float) -> None:
        """Drop a reading that came back too late to pace by, and stop reading
        for a backoff that doubles while reads stay slow."""
        self.slow_reads += 1
        self.last_slow_read_s = took
        self.read_holdoff_s = min(
            HOST_DMA_SERVO_READ_HOLDOFF_MAX_S,
            max(HOST_DMA_SERVO_READ_HOLDOFF_MIN_S, self.read_holdoff_s * 2),
        )
        self.read_holdoff_until = time.monotonic() + self.read_holdoff_s
        level = logging.DEBUG if self.slow_read_warned else logging.WARNING
        self.slow_read_warned = True
        # With the servo off only the stall re-anchor reads R, and there is no
        # pace correction to hold.
        consequence = (
            "holding the pace correction" if self._st.host_dma_servo else "not reading it again"
        )
        log.log(
            level,
            "audio: read-pointer read took %.0f ms, over the %.0f ms budget — %s "
            "for %.0f s (%d slow so far)",
            took * 1000.0,
            budget_s * 1000.0,
            consequence,
            self.read_holdoff_s,
            self.slow_reads,
        )

    def note_r_reading(self, r_addr: int) -> None:
        """Watch for an NMI consumer that stopped after a verified start.

        A consumer killed mid-session (a stray `#$7F` to `$DD0D`, a reset
        behind our back) otherwise presents as unexplained silence plus the
        fast playback the servo produces while chasing a dead reader. Warns
        once per session; the pacing behavior is untouched.

        The count is of readings, not chunks: behind a slow server they come
        a read backoff apart, so the warning reports the time R stood still.
        """
        now = time.monotonic()
        if r_addr == self.last_r_reading:
            self.r_stall_chunks += 1
        else:
            self.last_r_reading = r_addr
            self.last_r_reading_since = now
            self.r_stall_chunks = 0
        if self.r_stall_chunks >= NMI_STALL_WARN_CHUNKS and not self.stall_warned:
            self.stall_warned = True
            log.warning(
                "audio: NMI consumer stalled — R has not moved from $%04X for %.1f s "
                "(%d readings). Audio is silent and playback pace is unreliable from here.",
                r_addr,
                now - self.last_r_reading_since,
                self.r_stall_chunks + 1,
            )

    def observe_r_rate(self, r_addr: int) -> None:
        """Track the NMI consumer's byte rate dR/dt, from the R the gap servo
        already read. Observation only — nothing here steers anything.

        Runs whether or not ``update_rate_loop`` steers, and keeps the
        instantaneous per-chunk rate alongside the EMA. See
        docs/architecture/audio.md#host-dma-pitch-compensation--why-two-of-the-three-knobs-default-off.
        """
        alpha = NMI_RATE_LOOP_ACQUIRE_ALPHA if self.loop_acquiring else NMI_RATE_LOOP_EMA_ALPHA
        now = time.monotonic()
        # Taken on every reading, so it covers exactly the interval since the last.
        fastest_latch = self._timer.take_fastest_latch()
        if self.last_r_addr >= 0 and self.last_r_time > 0.0:
            dt = now - self.last_r_time
            dr = (r_addr - self.last_r_addr) % RING_BUFFER_SIZE
            # Discard a torn/backward read (half-ring jump = a read tear mid
            # self-modify, not real advance) — same guard as hostdma_drift_probe.
            # And discard any interval long enough for R to have wrapped: dr is
            # only known modulo the ring, so after a link stall a whole lap
            # plus a little reads as "a little" and seeds a rate far too low.
            if (
                dt > 0
                and dr < RING_BUFFER_SIZE // 2
                and dt < self.max_unambiguous_dt(fastest_latch)
            ):
                inst = dr / dt
                if self.r_rate_ema < 0:
                    self.r_rate_ema = inst
                else:
                    self.r_rate_ema += alpha * (inst - self.r_rate_ema)
                self.r_rate_min = inst if self.r_rate_min < 0 else min(self.r_rate_min, inst)
                self.r_rate_max = max(self.r_rate_max, inst)
        self.last_r_addr = r_addr
        self.last_r_time = now

    def max_unambiguous_dt(self, latch: int) -> float:
        """Longest interval between two R readings whose modular advance still
        has one reading: the time the consumer, at ``latch`` (the fastest armed
        over that interval, from ``NmiTimer.take_fastest_latch``), takes to
        cover half a ring (the torn-read guard's bound). Bus halts only slow R,
        so the fastest armed latch is the fastest it can have run. 0 (nothing
        armed) falls back to nominal."""
        latch = latch or self._timer.nominal_latch()
        return (RING_BUFFER_SIZE // 2) * (latch + 1) / cpu_clock(self._st.system)

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
        # Warm-up gate: the EMA keeps warming in observe_r_rate above, but the
        # latch holds at the seed and loop_chunk_count is left alone.
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
            # Not sample_rate: R runs on the latch grid, so targeting the
            # request would walk the latch off nominal by the quantization error.
            target_rate=timer.effective_rate,
        )
        if new_latch == timer.latch:
            # Within deadband or clamped at the ceiling: converged.
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

    def reset_for_consumer_start(self, ring_lead: int) -> None:
        """R only becomes meaningful once the NMI consumes; start the servo
        integrator + adaptive-rate loop clean and hold the rate loop at the
        seed until the start/seek transient settles (post-seek decode catch-up
        + the playlist's frame-drop snap), so it acquires from a steady R
        instead of chasing the spin-up reading. See NMI_RATE_LOOP_WARMUP_S.

        ``ring_lead`` is the prebuffer the consumer starts behind. With the
        servo off it stays the lead estimate, the gap holding near it."""
        self.ring_lead = float(ring_lead)
        self.integ = 0.0
        self.read_holdoff_until = 0.0
        self.read_holdoff_s = 0.0
        self.r_rate_ema = -1.0
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.reset_health_window()
        self.loop_chunk_count = 0
        self.loop_acquiring = True
        self.warmup_until = time.monotonic() + NMI_RATE_LOOP_WARMUP_S

    def resync(self, ring_lead: int) -> None:
        """The worker re-anchored W ``ring_lead`` bytes ahead of R after a
        stall. The integrator is kept: it is the standing bus-halt correction,
        which a stall does not change. The R-rate baseline is dropped (an
        interval spanning the stall says nothing about the consumer), and the
        adaptive loop's warm-up gate re-arms so it does not steer on the
        post-stall transient."""
        self.ring_lead = float(ring_lead)
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.note_disturbance()

    def reset_health_window(self) -> None:
        """Clear the per-window excursion trackers the streamer's health line
        reports. Called once per emitted line, and at consumer start where the
        window restarts — the point of a per-window line rather than a session
        total is that its numbers describe that window only."""
        self.health_gap_min = -1
        self.health_gap_max = -1
        self.r_rate_min = -1.0
        self.r_rate_max = -1.0

    def reset_run_telemetry(self) -> None:
        """Clear the per-run gap telemetry stop() summarizes. Distinct from
        reset_health_window on purpose: these span a whole run, those span one
        health window, and the class that owns the counters is where that
        distinction belongs — the streamer used to spell both out field by
        field from two methods that never mentioned each other."""
        self.gap_min = -1
        self.gap_max = -1
        self.gap_last = -1

    def reset_after_stop(self) -> None:
        """Clear the watchdog + adaptive-rate state so the next consumer
        re-acquires from nominal rather than carrying a stale R-rate
        estimate."""
        self.last_r_reading = -1
        self.last_r_reading_since = 0.0
        self.r_stall_chunks = 0
        self.stall_warned = False
        self.slow_reads = 0
        self.slow_read_warned = False
        self.ring_lead = -1.0
        self.r_rate_ema = -1.0
        self.last_r_addr = -1
        self.last_r_time = 0.0
        self.loop_chunk_count = 0
        self.loop_acquiring = True
