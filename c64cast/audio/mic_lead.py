"""Closed loop on the REU mic pump's lead (#560).

The REU mic path has three clocks: the host mic (exact sample_rate), the C64
pump that REC-DMAs the REU mic ring into the NMI ring, and the NMI reader. The
host's write head starts ``REU_MIC_BOOTSTRAP_BYTES`` ahead of the pump's src
tracker and nothing else ties the two together, so the lead drifts with the
pump's achieved rate: it grows ~1.8 KB/s under REU-staged mhires (the bank-swap
halts cost the C64 ticks) and shrinks ~32 B/s under petscii (CIA latch
quantization), until the host laps the pump or the pump overtakes the host.

Two collaborators of ``AudioStreamer`` close that loop:

* ``MicLeadServo`` — a thread that reads ``$C025-$C204`` about once a second
  (REST, off the audio callback): the NMI read pointer and the pump's src and
  dst trackers, in one read that both loops share (#603). It turns
  the lead error into a drop fraction through the shared ``pi_step``, asks for
  a re-anchor when the lead reads as overtaken or far past the target, and
  falls back to open loop — loudly — when the reads keep failing.
* ``MicRingGovernor`` — the second stage (#580): the pump's lead over the
  NMI reader in the $4000 ring. The reader loses ticks to bus halts and the
  pump does not, so the pump laps it unless something slows the pump down.
  Ticked from the servo's thread with R and the pump's dst tracker from that
  shared read, it trims the pump's CIA #1 latch through the shared
  ``pi_step``; the host loop above then follows the slower pump.
* ``MicLeadShaper`` — callback-side, stateful: applies the drop fraction to
  the sample stream. Within ``±MIC_LEAD_RESAMPLE_MAX`` it resamples (single
  samples dropped or repeated, interpolated; the pitch moves by that
  fraction). A larger drop switches it to short crossfaded splices for the
  whole correction, evenly spaced by the steady accrual of the drop, so the
  pitch stays exact and content is skipped instead.

**Threading contract.** ``MicLeadServo`` and ``MicRingGovernor`` fields are
written by the servo's thread;
the audio callback reads ``drop_frac`` (one float, atomic) and claims a
re-anchor through ``take_reanchor`` (under ``_lock``). ``MicLeadShaper`` is
touched only by the audio callback.

See docs/architecture/audio.md#mic_leadpy--reu-mic-lead-servo.
"""

from __future__ import annotations

import enum
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

import numpy as np

from c64cast.hw.c64 import CIA_TIMER_LATCH_MAX

from .audio_handlers import (
    READ_PTR_LO_ADDR,
    REU_AUDIO_DST_TRACKER_ADDR,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_MIC_BASE,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_END,
    REU_MIC_RING_LEAD,
    REU_MIC_SIZE,
    REU_PUMP_CHUNK_SIZE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
)
from .audio_servo import (
    pi_step,
)

log = logging.getLogger(__name__)

# One pump read per interval. The PI gains below are per interval.
MIC_LEAD_SERVO_INTERVAL_S = 1.0
# Proportional gain: the fraction of the lead error corrected per second.
# The tracker reads quantize to the 128-byte pump chunk, so read jitter moves
# the drop fraction by about ±0.4 % at 12 kHz.
MIC_LEAD_KP = 0.4
# Integral gain per interval; the integrator carries the steady rate mismatch
# (~15 % under mhires). With KP the sampled loop's poles are real (≈0.87 and
# ≈0.69), so the lead settles without ringing.
MIC_LEAD_KI = 0.04
# Largest drop fraction the shaper resamples: 3 % is about half a semitone,
# the largest pitch move this loop may make. Past it the shaper switches to
# splices for the whole correction and resamples nothing, so the pitch is
# exact; it switches back below MIC_LEAD_SPLICE_EXIT, the gap between the two
# keeping a correction that hovers near the cap from flapping the pitch.
MIC_LEAD_RESAMPLE_MAX = 0.03
MIC_LEAD_SPLICE_EXIT = 0.01
# Ceiling on the total drop fraction. Covers the measured mhires deficit
# (~17 %) with headroom; the integrator's anti-windup is sized from it.
MIC_LEAD_MAX_DROP = 0.35
# A lead past this (or below zero) is a lap or an overtake rather than drift,
# and is re-anchored instead of steered: 8 KB is ~0.7 s at 12 kHz.
MIC_LEAD_REANCHOR_ABOVE = REU_MIC_SIZE // 8
# A re-anchor NEUTRAL-fills from this far short of the pump's estimated
# position. The estimate is extrapolated from the midpoint of the read the
# measurement used, so its error is about half that read's round trip at the
# pump's rate: 256 B covers a round trip of ~40 ms at 12 kHz. Past that, an
# estimate ahead of the pump leaves the pump that many bytes of the overtaken
# or lapped ring to play before the fill.
MIC_LEAD_REANCHOR_GUARD = 2 * REU_PUMP_CHUNK_SIZE
# Two readings' tracker phases (``tracker_phase``) must agree this closely,
# else one came back garbled. A read that lands inside the pump's own lo/mi
# carry, or between its src and dst advances, is off by at most a chunk plus
# 256 B and passes.
MIC_LEAD_TORN_TOLERANCE = 1024
# A re-anchor the callback has not claimed within this many intervals is
# dropped (the first with a warning, later ones at debug), and measuring resumes: a callback that has stopped
# reaching it (every block flagged by PortAudio) must not freeze the loop.
MIC_LEAD_REANCHOR_CLAIM_INTERVALS = 3
# Consecutive failed measurements before the loop opens (drop fraction 0).
MIC_LEAD_OPEN_LOOP_AFTER = 3
# Per pump read. requests applies it to each phase (connect, then each
# socket read), so one read can take about twice it. _measure skips a
# confirming read once stop() is set, so the join waits for at most one read.
MIC_LEAD_READ_TIMEOUT_S = 0.5
MIC_LEAD_JOIN_TIMEOUT_S = 2 * MIC_LEAD_READ_TIMEOUT_S + 0.5
# While the loop is open the interval doubles per failed measurement up to
# this, so a device that stopped answering is not dialed twice a second.
MIC_LEAD_OPEN_LOOP_MAX_WAIT_S = 8.0
# Splice geometry, in milliseconds of input. A splice removes about
# SPLICE_MS of content; the exact cut is searched within ±SEARCH_MS for the
# best waveform match, then joined with a FADE_MS linear crossfade.
MIC_SPLICE_MS = 30.0
MIC_SPLICE_SEARCH_MS = 10.0
MIC_SPLICE_FADE_MS = 8.0
# The ring governor's plant has the lead servo's shape (a lead that integrates
# a rate mismatch, steered by a fraction of the nominal rate), so it runs on the
# same gains and settles as the lead does. The cascade is stable because the
# host loop measures the pump's rate every interval and follows it.
MIC_RING_KP = MIC_LEAD_KP
MIC_RING_KI = MIC_LEAD_KI
# How far the governor may stretch the pump's period. The reader has measured
# 5.7 % short of the pump under petscii and 1.5 % under mhires on a U64-II, so
# 25 % is headroom; past it, the host loop would be splicing a quarter of the
# input away. The pump runs faster than matched only when the reader is the
# faster one, which bus halts cannot cause, so that side stays narrow.
MIC_RING_MAX_SLOW = 0.25
MIC_RING_MAX_FAST = 0.03
# The lead is known only modulo the ring, so one split point decides whether a
# large reading is a pump far ahead or a reader that overran the pump. Bus
# halts slow only the reader, so the pump running ahead is the drift there is;
# an overrun can come only from a pump burst short of the reader's, which moved
# the lead about 1 KB in a second under mhires. A reading within this many
# bytes behind the reader is an overrun, and any other is the pump ahead.
MIC_RING_OVERRUN_WINDOW = 1024


class TrimWrite(enum.Enum):
    """What became of one CIA #1 trim the governor sent."""

    DELIVERED = enum.auto()
    # Sent, but the backend's delivery_epoch moved across it, so the link may
    # have dropped it: the governor sends its next latch even when unchanged.
    UNCONFIRMED = enum.auto()
    # The pump it governs is disarmed; the governor writes nothing more.
    REFUSED = enum.auto()


# The one span read per interval: the NMI read pointer at $C025 through the
# pump's src (LO/MI/HI) and dst (LO/HI) trackers at $C200-$C204, so all three
# come from the same instant and their differences carry no round-trip skew.
MIC_PUMP_SPAN_ADDR = READ_PTR_LO_ADDR
MIC_PUMP_SPAN_LEN = REU_AUDIO_DST_TRACKER_ADDR + 2 - READ_PTR_LO_ADDR


class MicPumpReading(NamedTuple):
    """One span read of the mic pump's pointers."""

    src: int  # the pump's src tracker, as an offset into the REU mic ring
    r: int  # the NMI read pointer, a C64 address in the $4000 ring
    w: int  # the pump's dst tracker, a C64 address in the $4000 ring


def read_mic_pump(
    read_memory: Callable[..., bytes | None], timeout: float
) -> MicPumpReading | None:
    """Read and decode ``$C025-$C204``, or None when the read fails (or the
    backend raises, as one without reads does) or any pointer is outside its
    ring. ``timeout`` is the backend's per-read bound."""
    try:
        raw = read_memory(MIC_PUMP_SPAN_ADDR, MIC_PUMP_SPAN_LEN, timeout=timeout)
    except Exception as e:
        log.debug("audio[reu mic]: pump pointer read failed: %s", e)
        return None
    if raw is None or len(raw) != MIC_PUMP_SPAN_LEN:
        return None
    trk = REU_AUDIO_SRC_TRACKER_ADDR - MIC_PUMP_SPAN_ADDR
    r = raw[0] | (raw[1] << 8)
    src = raw[trk] | (raw[trk + 1] << 8) | (raw[trk + 2] << 16)
    w = raw[trk + 3] | (raw[trk + 4] << 8)
    if not (
        REU_MIC_BASE <= src <= REU_MIC_END
        and RING_BUFFER_ADDR <= r < RING_BUFFER_END
        and RING_BUFFER_ADDR <= w < RING_BUFFER_END
    ):
        return None
    return MicPumpReading((src - REU_MIC_BASE) % REU_MIC_SIZE, r, w)


def tracker_phase(reading: MicPumpReading) -> int:
    """The src tracker's offset less the dst tracker's, modulo the $4000
    ring. The pump advances both by one chunk per tick and the 64 KB mic ring
    is a whole number of $4000 rings, so this holds still for a session while
    nothing rewrites dst, and a reading whose phase moved came back garbled."""
    return (reading.src - (reading.w - RING_BUFFER_ADDR)) % RING_BUFFER_SIZE


assert REU_MIC_SIZE % RING_BUFFER_SIZE == 0, (
    "tracker_phase holds still only while the mic ring is a whole number of $4000 rings"
)


def mic_lead_correction(
    lead: int,
    integ: float,
    *,
    sample_rate: int,
    target: int = REU_MIC_BOOTSTRAP_BYTES,
) -> tuple[float, float]:
    """One mic-lead servo decision: ``(drop_frac, new_integ)``.

    ``lead`` is the host write head's signed lead over the pump's src tracker,
    in bytes (= samples). A positive ``drop_frac`` is the fraction of input
    samples to remove (lead too large); negative repeats samples, and only as
    far as the resampler may go. Pure, for the unit tests."""
    rate = float(sample_rate)
    return pi_step(
        lead - target,
        integ,
        kp=MIC_LEAD_KP / rate,
        ki=MIC_LEAD_KI / rate,
        # The integrator's contribution spans exactly the output range: wider
        # on the negative side, it would wind up while the output sits pinned
        # at -MIC_LEAD_RESAMPLE_MAX and hold the drop there after the
        # mismatch ends.
        integ_min=-MIC_LEAD_RESAMPLE_MAX * rate / MIC_LEAD_KI,
        integ_max=MIC_LEAD_MAX_DROP * rate / MIC_LEAD_KI,
        out_min=-MIC_LEAD_RESAMPLE_MAX,
        out_max=MIC_LEAD_MAX_DROP,
    )


def mic_lead_rate_seed(pump_rate: float, *, sample_rate: int) -> tuple[float, float]:
    """``(drop_frac, integ)`` for a loop whose lead was just reset to the
    target: the drop that matches the host to ``pump_rate`` (bytes/s), and the
    integrator that holds it with no error left for the proportional term.
    A re-anchor jumps the lead but not the rate mismatch, so the loop restarts
    from what the pump is doing now rather than from what it was steering
    before the jump. Clamped like ``mic_lead_correction``'s output, so the
    integrator stays inside its anti-windup bounds. A rate that is not a
    positive finite number says nothing about the pump and seeds the startup
    state ``(0.0, 0.0)``, as an idle tracker stops steering: NaN would
    otherwise fall through the clamp to the full drop, the dangerous side."""
    if not (math.isfinite(pump_rate) and pump_rate > 0.0):
        return 0.0, 0.0
    rate = float(sample_rate)
    need = max(-MIC_LEAD_RESAMPLE_MAX, min(MIC_LEAD_MAX_DROP, 1.0 - pump_rate / rate))
    return need, need * rate / MIC_LEAD_KI


def signed_ring_lead(lead: int) -> int:
    """The pump's lead over the NMI reader, from its value modulo the ring:
    one within ``MIC_RING_OVERRUN_WINDOW`` of a full ring is the reader past
    the pump (negative), and any other is the pump that far ahead."""
    lead %= RING_BUFFER_SIZE
    if lead >= RING_BUFFER_SIZE - MIC_RING_OVERRUN_WINDOW:
        lead -= RING_BUFFER_SIZE
    return lead


def mic_ring_correction(
    lead: int,
    integ: float,
    *,
    sample_rate: int,
    target: int = REU_MIC_RING_LEAD,
) -> tuple[float, float]:
    """One ring-governor decision: ``(slow_frac, new_integ)``.

    ``lead`` is the pump's dst tracker less the NMI read pointer, read as
    ``signed_ring_lead`` does. A positive ``slow_frac`` stretches the pump's
    period by that fraction (the pump is ahead); negative shortens it. Pure,
    for the tests."""
    rate = float(sample_rate)
    error = signed_ring_lead(lead) - target
    return pi_step(
        error,
        integ,
        kp=MIC_RING_KP / rate,
        ki=MIC_RING_KI / rate,
        integ_min=-MIC_RING_MAX_FAST * rate / MIC_RING_KI,
        integ_max=MIC_RING_MAX_SLOW * rate / MIC_RING_KI,
        out_min=-MIC_RING_MAX_FAST,
        out_max=MIC_RING_MAX_SLOW,
    )


def trimmed_pump_latch(matched_latch: int, slow_frac: float) -> int:
    """The CIA #1 latch that stretches the matched pump period by
    ``slow_frac``. The period is ``latch + 1`` cycles, about 10.9 k at 12 kHz,
    so one latch step is ~0.01 % of the rate. Held to the 16-bit timer."""
    period = (matched_latch + 1) * (1.0 + slow_frac)
    return max(1, min(CIA_TIMER_LATCH_MAX, round(period) - 1))


def mic_lead_in_range(lead: int) -> bool:
    """False for a lead the servo re-anchors instead of steering: below zero
    (the pump overtook the write head) or past ``MIC_LEAD_REANCHOR_ABOVE``."""
    return 0 <= lead <= MIC_LEAD_REANCHOR_ABOVE


def reanchor_fill(anchor: int) -> tuple[int, int]:
    """Where a re-anchor restarts the write head, and how many NEUTRAL bytes
    it writes there first: ``(pos, fill_len)``. ``anchor`` is the pump's
    estimated src offset (``MicLeadServo.take_reanchor``); the fill starts
    ``MIC_LEAD_REANCHOR_GUARD`` short of it and ends ``REU_MIC_BOOTSTRAP_BYTES``
    past it, the same lead the session starts with."""
    pos = (anchor - MIC_LEAD_REANCHOR_GUARD) % REU_MIC_SIZE
    return pos, REU_MIC_BOOTSTRAP_BYTES + MIC_LEAD_REANCHOR_GUARD


def signed_ring_delta(a: int, b: int, ring: int = REU_MIC_SIZE) -> int:
    """``a - b`` on a ring of ``ring`` bytes, in ``[-ring/2, ring/2)``."""
    d = (a - b) % ring
    return d - ring if d >= ring // 2 else d


def best_splice_cut(buf: np.ndarray, fade: int, cut_min: int, cut_max: int) -> int:
    """The cut length in ``[cut_min, cut_max]`` whose landing window best
    matches ``buf[:fade]`` (normalized cross-correlation), so the crossfade
    joins two stretches of waveform that are already in phase. Falls back to
    the middle of the range on silence. ``buf`` must hold ``cut_max + fade``."""
    head = buf[:fade].astype(np.float64)
    windows = np.lib.stride_tricks.sliding_window_view(
        buf[cut_min : cut_max + fade].astype(np.float64), fade
    )
    energy = np.sqrt((windows**2).sum(axis=1) * float((head**2).sum()))
    if not np.any(energy > 1e-12):
        return (cut_min + cut_max) // 2
    score = np.where(energy > 1e-12, windows @ head / np.maximum(energy, 1e-12), -np.inf)
    return cut_min + int(np.argmax(score))


class MicLeadShaper:
    """Applies the servo's drop fraction to the mic sample stream (callback
    thread only)."""

    def __init__(self, sample_rate: int) -> None:
        ms = sample_rate / 1000.0
        self.fade = max(2, round(MIC_SPLICE_FADE_MS * ms))
        nominal = round(MIC_SPLICE_MS * ms)
        search = round(MIC_SPLICE_SEARCH_MS * ms)
        self.cut_min = max(1, nominal - search)
        self.cut_max = nominal + search
        self.splice_len = nominal
        self._ramp = np.linspace(0.0, 1.0, self.fade, dtype=np.float32)
        self._held = np.zeros(0, dtype=np.float32)
        self._splice_debt = 0.0
        self.splicing = False
        self._prev = 0.0
        self._phase = 0.0
        self.splices = 0
        self.skipped_samples = 0

    def process(self, x: np.ndarray, drop_frac: float) -> np.ndarray:
        """Return ``x`` with ``drop_frac`` of its samples removed (or, when
        negative, repeated). May hold back up to ``cut_max + fade`` samples
        while a splice waits for enough input to search."""
        x = np.asarray(x, dtype=np.float32)
        if drop_frac > MIC_LEAD_RESAMPLE_MAX:
            self.splicing = True
        elif drop_frac < MIC_LEAD_SPLICE_EXIT:
            self.splicing = False
        if self.splicing:
            self._splice_debt += drop_frac * len(x)
            resample = 0.0
        else:
            self._splice_debt = 0.0
            resample = max(-MIC_LEAD_RESAMPLE_MAX, min(MIC_LEAD_RESAMPLE_MAX, drop_frac))
        return self._resample(self._splice(x), resample)

    def _splice(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self._held, x)) if len(self._held) else x
        parts: list[np.ndarray] = []
        need = self.cut_max + self.fade
        while self._splice_debt >= self.splice_len and len(buf) >= need:
            cut = best_splice_cut(buf, self.fade, self.cut_min, self.cut_max)
            a = buf[: self.fade]
            b = buf[cut : cut + self.fade]
            parts.append(a + (b - a) * self._ramp)
            buf = buf[cut + self.fade :]
            self._splice_debt -= cut
            self.splices += 1
            self.skipped_samples += cut
        if self._splice_debt >= self.splice_len:
            # Pending: keep the rest so the next block can complete the search.
            self._held = buf
        else:
            parts.append(buf)
            self._held = np.zeros(0, dtype=np.float32)
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts) if len(parts) > 1 else parts[0]

    def _resample(self, x: np.ndarray, drop_frac: float) -> np.ndarray:
        """Linear-interpolating resample by ``1 - drop_frac``, continuous
        across calls: the read position and the previous block's last sample
        carry over, so a block edge is no different from any other sample."""
        n = len(x)
        if n == 0:
            return x
        step = 1.0 / (1.0 - drop_frac)
        count = math.ceil((n - self._phase) / step)
        y = np.empty(n + 1, dtype=np.float32)
        y[0] = self._prev
        y[1:] = x
        self._prev = float(x[-1])
        if count <= 0:
            self._phase -= n
            return np.zeros(0, dtype=np.float32)
        pos = self._phase + step * np.arange(count)
        self._phase = float(pos[-1] + step - n)
        out: np.ndarray = np.interp(pos, np.arange(n + 1), y).astype(np.float32)
        return out


class _Measurement(NamedTuple):
    """One pump read with the host's lead over it."""

    lead: int  # the host write head's signed lead over the pump's src tracker
    reading: MicPumpReading
    at: float  # the servo clock at the middle of the read


class MicLeadServo:
    """The 1 Hz closed loop on the host's lead over the REU mic pump."""

    def __init__(
        self,
        *,
        read_memory: Callable[..., bytes | None],
        write_pos: Callable[[], int],
        sample_rate: int,
        interval_s: float = MIC_LEAD_SERVO_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        ring_governor: MicRingGovernor | None = None,
    ) -> None:
        self._clock = clock
        self.ring_governor = ring_governor
        self._read = read_memory
        self._write_pos = write_pos
        self._rate = sample_rate
        self._interval = interval_s
        self.drop_frac = 0.0
        self._integ = 0.0
        self._lock = threading.Lock()
        self._reanchor: tuple[int, float, float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fails = 0
        self._open_loop = False
        self._last_pump: tuple[int, float] | None = None
        # The tracker phase of the last reading this servo trusted.
        self._tracker_phase: int | None = None
        self._pump_rate = float(sample_rate)
        self.lead_min: int | None = None
        self.lead_max: int | None = None
        self.reanchors = 0
        self.reanchors_dropped = 0
        self.open_loop_spells = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mic-lead-servo", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """End the loop at its next wait without joining. The streamer calls
        this before its teardown, which can outlast the re-anchor claim window
        on a stalled link; stop() still joins afterwards."""
        self._stop.set()

    def stop(self, timeout: float = MIC_LEAD_JOIN_TIMEOUT_S) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning("audio[reu mic]: lead servo did not exit within %.1fs", timeout)

    def take_reanchor(self) -> int | None:
        """Claim a pending re-anchor: the pump's estimated src offset now (by
        the servo's clock), extrapolated from the measurement at the pump's measured
        rate. None when nothing is pending."""
        with self._lock:
            req, self._reanchor = self._reanchor, None
        if req is None:
            return None
        pump, measured_at, rate = req
        return round(pump + rate * max(0.0, self._clock() - measured_at)) % REU_MIC_SIZE

    def _run(self) -> None:
        while not self._stop.wait(self._next_wait()):
            try:
                ring = self.tick()
            except Exception:
                # A defect here must not kill the thread silently or take the
                # loop's last output with it: open the loop and say so.
                self.drop_frac = 0.0
                log.exception("audio[reu mic]: lead servo step failed; running open-loop")
                return
            self._tick_ring_governor(ring)

    def _tick_ring_governor(self, ring: tuple[int, int] | None) -> None:
        """Step the ring governor on ``(R, W)`` from this interval's read
        (None when it had no usable one), if there is a governor and the loop
        is still running. One that raises is retired at the latch it last
        wrote, which holds the correction it had reached, and the host loop
        carries on.

        While this loop is open its wait backs off to as much as
        ``MIC_LEAD_OPEN_LOOP_MAX_WAIT_S``, and the governor's gains are per
        ``interval_s``: stepped every 8 s they would correct eight intervals'
        worth of lead per step and oscillate into a lap. So an open loop holds
        the latch, as a failed read does."""
        gov = self.ring_governor
        if gov is None or gov.retired or self._open_loop or self._stop.is_set():
            return
        try:
            gov.tick(ring)
        except Exception:
            gov.retired = True
            log.exception(
                "audio[reu mic]: ring governor step failed; holding the pump at CIA #1 latch %d",
                gov.latch,
            )

    def _next_wait(self) -> float:
        if not self._open_loop:
            return self._interval
        doublings = min(self._fails - MIC_LEAD_OPEN_LOOP_AFTER + 1, 16)
        ceiling = max(self._interval, MIC_LEAD_OPEN_LOOP_MAX_WAIT_S)
        return min(self._interval * 2.0**doublings, ceiling)

    def tick(self) -> tuple[int, int] | None:
        """One measurement and decision. Returns ``(R, W)`` from the read for
        the ring governor, or None when there was no usable read. The read is
        made even while a re-anchor waits for the callback, which leaves the
        host loop alone, because the governor steps on it. Public for the
        tests, which drive it without the thread."""
        steer = True
        with self._lock:
            pending = self._reanchor
            if pending is not None:
                if self._clock() - pending[1] <= MIC_LEAD_REANCHOR_CLAIM_INTERVALS * self._interval:
                    steer = False  # the callback has not applied the last one yet
                else:
                    self._reanchor = None
        if pending is not None and steer:
            self.reanchors_dropped += 1
            (log.warning if self.reanchors_dropped == 1 else log.debug)(
                "audio[reu mic]: the mic callback has not taken a re-anchor in %.0fs; "
                "dropping it and measuring again",
                MIC_LEAD_REANCHOR_CLAIM_INTERVALS * self._interval,
            )
        m = self._measure()
        # A read that finished inside the teardown, the confirming one or the
        # re-anchor's re-read included, is discarded: a re-anchor posted from
        # it is one the callback no longer takes. A stop is not a failure.
        if self._stop.is_set():
            return None
        if m is None:
            self._note_failure()
            return None
        # Counted whether or not it steers: a re-anchor's wait must not leave
        # failures around a good read looking consecutive.
        self._note_success()
        if steer:
            self._steer(m)
        return m.reading.r, m.reading.w

    def _steer(self, m: _Measurement) -> None:
        lead, pump, at = m.lead, m.reading.src, m.at
        last, self._last_pump = self._last_pump, (pump, at)
        # A re-anchor reseeds the loop from the fastest of three rates: the
        # one the integrator already holds the host to, the rate average
        # before this interval, and this interval's own. A lap or an overtake
        # moves the lead, not the rate mismatch, and the integrator is the
        # loop's slow estimate of that mismatch. A stall reads the pump as slow
        # (and ends in a lap), but barely moves the integrator, and a lap
        # reseeds it to hold the host to a rate no slower than before, so no
        # stall, however many ticks it spans, sets the seed. A speed-up reads
        # the pump as fast, where the integrator is the stale one: when it ends
        # in an overtake, this interval's rate carries it; when the loop
        # absorbs it and a stall laps a few ticks later, the average has
        # already caught it. The slower reading is the one not to trust: an
        # over-drop has the ~1.6 KB target to fall through zero, an under-drop
        # ~6.6 KB to the lap limit.
        seed_rate = max(self._rate - MIC_LEAD_KI * self._integ, self._pump_rate)
        if last is not None:
            advanced = (pump - last[0]) % REU_MIC_SIZE
            if advanced == 0:
                # Pump not running (teardown, or a dead pump): nothing to steer.
                self.drop_frac = 0.0
                log.debug("audio[reu mic]: pump src tracker idle at +%d", pump)
                return
            if at > last[1]:
                measured = advanced / (at - last[1])
                self._pump_rate += 0.5 * (measured - self._pump_rate)
                seed_rate = max(seed_rate, measured)
        self.lead_min = lead if self.lead_min is None else min(self.lead_min, lead)
        self.lead_max = lead if self.lead_max is None else max(self.lead_max, lead)
        if not mic_lead_in_range(lead):
            self.reanchors += 1
            # The fill restarts the lead at the target, so the drop that steered
            # toward this jump is stale: a pump that sped up mid-scene would keep
            # being over-dropped from, overtake again, and re-anchor every few
            # seconds, each one a NEUTRAL dropout.
            self.drop_frac, self._integ = mic_lead_rate_seed(seed_rate, sample_rate=self._rate)
            with self._lock:
                self._reanchor = (pump, at, self._pump_rate)
            (log.warning if self.reanchors == 1 else log.debug)(
                "audio[reu mic]: write head %s the pump (lead %+d B); re-anchoring "
                "%d B ahead with a NEUTRAL fill",
                "overtaken by" if lead < 0 else "too far ahead of",
                lead,
                REU_MIC_BOOTSTRAP_BYTES,
            )
            return
        self.drop_frac, self._integ = mic_lead_correction(lead, self._integ, sample_rate=self._rate)
        log.debug("audio[reu mic]: lead %+d B → drop %.4f", lead, self.drop_frac)

    def _host_between(self, before: int, after: int) -> int:
        return (before + ((after - before) % REU_MIC_SIZE) // 2) % REU_MIC_SIZE

    def _read_once(self) -> _Measurement | None:
        """One span read, against the host position midway across it and
        stamped at its middle: the pump position was sampled somewhere inside
        the read, and the midpoint halves the worst-case error of stamping it
        at either end."""
        h0 = self._write_pos()
        t0 = self._clock()
        reading = read_mic_pump(self._read, MIC_LEAD_READ_TIMEOUT_S)
        t1 = self._clock()
        h1 = self._write_pos()
        if reading is None:
            return None
        lead = signed_ring_delta(self._host_between(h0, h1), reading.src)
        return _Measurement(lead, reading, (t0 + t1) / 2)

    def _phase_agrees(self, reading: MicPumpReading, phase: int | None) -> bool:
        if phase is None:
            return False
        delta = signed_ring_delta(tracker_phase(reading), phase, RING_BUFFER_SIZE)
        return abs(delta) <= MIC_LEAD_TORN_TOLERANCE

    def _measure(self) -> _Measurement | None:
        """One read, whose tracker phase must agree with the last trusted
        reading's. With none trusted yet, or a reading that disagrees, a
        second read is made at once and has to agree with the trusted phase
        or with the first read, else the measurement is torn and counts as a
        failure. Two reads that agree with each other but not with the
        trusted phase replace it.

        The phase is taken modulo the $4000 ring, so it cannot see a src
        tracker off by a whole number of those rings, and such a reading puts
        the lead 8 KB high or low: past the re-anchor limits either way. So a
        lead that would re-anchor is read once more and must agree within
        ``MIC_LEAD_TORN_TOLERANCE`` plus the host's rate times the gap between
        the reads, and keep the trusted phase, else the measurement is torn. That costs
        a read only when the host has really lapped or been overtaken."""
        m = self._read_once()
        if m is None or self._stop.is_set():
            return None
        if not self._phase_agrees(m.reading, self._tracker_phase):
            first = m
            m = self._read_once()
            if m is None:
                return None
            if not (
                self._phase_agrees(m.reading, self._tracker_phase)
                or self._phase_agrees(m.reading, tracker_phase(first.reading))
            ):
                log.debug(
                    "audio[reu mic]: torn pump read (tracker phase %d vs %d)",
                    tracker_phase(first.reading),
                    tracker_phase(m.reading),
                )
                return None
        self._tracker_phase = tracker_phase(m.reading)
        if mic_lead_in_range(m.lead):
            return m
        if self._stop.is_set():
            return None
        check = self._read_once()
        if check is None:
            return None
        if not self._phase_agrees(check.reading, self._tracker_phase):
            log.debug(
                "audio[reu mic]: torn pump read (tracker phase %d vs %d on the re-read)",
                self._tracker_phase,
                tracker_phase(check.reading),
            )
            return None
        # The lead moves between the reads by the host's advance less the
        # pump's, up to the host's rate on a stalled pump, so the tolerance
        # grows with the gap. Capped at half a $4000 ring, it still tells the
        # 8 KB a garbled src tracker is off by from that motion.
        moved = round(self._rate * max(0.0, check.at - m.at))
        tolerance = min(MIC_LEAD_TORN_TOLERANCE + moved, RING_BUFFER_SIZE // 2)
        if abs(signed_ring_delta(check.lead, m.lead)) > tolerance:
            log.debug(
                "audio[reu mic]: torn pump read (lead %+d vs %+d on the re-read)",
                m.lead,
                check.lead,
            )
            return None
        return check

    def _note_failure(self) -> None:
        # The next good read must not measure the pump's rate across the gap:
        # past ~5 s the pump has gone round the ring and the delta aliases.
        self._last_pump = None
        self._fails += 1
        if self._fails >= MIC_LEAD_OPEN_LOOP_AFTER and not self._open_loop:
            self._open_loop = True
            self.open_loop_spells += 1
            self.drop_frac = 0.0
            (log.warning if self.open_loop_spells == 1 else log.info)(
                "audio[reu mic]: %d pump-tracker reads in a row failed; the lead servo "
                "is open-loop, so mic latency drifts until the reads recover",
                self._fails,
            )

    def _note_success(self) -> None:
        if self._open_loop:
            (log.info if self.open_loop_spells == 1 else log.debug)(
                "audio[reu mic]: pump-tracker reads recovered; lead servo closed again"
            )
        self._fails = 0
        self._open_loop = False


class MicRingGovernor:
    """The 1 Hz closed loop on the pump's lead over the NMI reader (#580).

    ``tick`` takes ``(R, W)`` from the lead servo's read, or None.
    ``write_latch`` writes a CIA #1 latch and says what became of it
    (``TrimWrite``). Once it is refused, because the pump it governs has been
    disarmed, the governor writes nothing more: a write landing after the
    teardown's kernal-latch restore would leave the jiffy IRQ at the pump's
    rate for every later scene. A write the link may have dropped is sent
    again at the next tick, whatever latch that tick asks for."""

    def __init__(
        self,
        *,
        write_latch: Callable[[int], TrimWrite],
        matched_latch: int,
        sample_rate: int,
    ) -> None:
        self._write_latch = write_latch
        self._matched = matched_latch
        self._rate = sample_rate
        self._integ = 0.0
        self.slow_frac = 0.0
        self.latch = matched_latch
        # The pump arm confirmed the matched latch before the governor exists.
        self._latch_delivered = True
        self.retired = False
        self.lead_min: int | None = None
        self.lead_max: int | None = None
        self.slow_min: float | None = None
        self.slow_max: float | None = None
        self.failed_reads = 0
        self.unconfirmed_trims = 0

    def tick(self, ring: tuple[int, int] | None) -> None:
        """One decision on ``(R, W)``. A failed read (None) holds the latch:
        the integrator is the standing bus-halt correction, and dropping it
        would hand back the drift that laps the ring."""
        if self.retired:
            return
        if ring is None:
            self.failed_reads += 1
            return
        r, w = ring
        # Signed, so an overrun shows in the stop() summary as the negative
        # lead it is rather than as a near-full ring.
        lead = signed_ring_lead(w - r)
        self.lead_min = lead if self.lead_min is None else min(self.lead_min, lead)
        self.lead_max = lead if self.lead_max is None else max(self.lead_max, lead)
        self.slow_frac, self._integ = mic_ring_correction(lead, self._integ, sample_rate=self._rate)
        self.slow_min = (
            self.slow_frac if self.slow_min is None else min(self.slow_min, self.slow_frac)
        )
        self.slow_max = (
            self.slow_frac if self.slow_max is None else max(self.slow_max, self.slow_frac)
        )
        latch = trimmed_pump_latch(self._matched, self.slow_frac)
        log.debug(
            "audio[reu mic]: C64 ring lead %d B → pump slowed %.4f (CIA #1 latch %d)",
            lead,
            self.slow_frac,
            latch,
        )
        if latch == self.latch and self._latch_delivered:
            return
        sent = self._write_latch(latch)
        if sent is TrimWrite.REFUSED:
            self.retired = True
            return
        self.latch = latch
        self._latch_delivered = sent is TrimWrite.DELIVERED
        if not self._latch_delivered:
            self.unconfirmed_trims += 1
