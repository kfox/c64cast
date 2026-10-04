"""Closed loop on the REU mic pump's lead (#560).

The REU mic path has three clocks: the host mic (exact sample_rate), the C64
pump that REC-DMAs the REU mic ring into the NMI ring, and the NMI reader. The
host's write head starts ``REU_MIC_BOOTSTRAP_BYTES`` ahead of the pump's src
tracker and nothing else ties the two together, so the lead drifts with the
pump's achieved rate: it grows ~1.8 KB/s under REU-staged mhires (the bank-swap
halts cost the C64 ticks) and shrinks ~32 B/s under petscii (CIA latch
quantization), until the host laps the pump or the pump overtakes the host.

Two collaborators of ``AudioStreamer`` close that loop:

* ``MicLeadServo`` — a thread that reads the pump's src tracker at
  ``$C200-$C202`` about once a second (REST, off the audio callback), turns
  the lead error into a drop fraction through the shared ``pi_step``, asks for
  a re-anchor when the lead reads as overtaken or far past the target, and
  falls back to open loop — loudly — when the reads keep failing.
* ``MicLeadShaper`` — callback-side, stateful: applies the drop fraction to
  the sample stream. Within ``±MIC_LEAD_RESAMPLE_MAX`` it resamples (single
  samples dropped or repeated, interpolated; the pitch moves by that
  fraction). A larger drop switches it to short crossfaded splices for the
  whole correction, evenly spaced by the steady accrual of the drop, so the
  pitch stays exact and content is skipped instead.

**Threading contract.** ``MicLeadServo`` fields are written by its own thread;
the audio callback reads ``drop_frac`` (one float, atomic) and claims a
re-anchor through ``take_reanchor`` (under ``_lock``). ``MicLeadShaper`` is
touched only by the audio callback.

See docs/architecture/audio.md#mic_leadpy--reu-mic-lead-servo.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

import numpy as np

from .audio_handlers import (
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_MIC_BASE,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_END,
    REU_MIC_SIZE,
    REU_PUMP_CHUNK_SIZE,
    pi_step,
)

log = logging.getLogger(__name__)

# One tracker read pair per interval. The PI gains below are per interval.
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
# position. The estimate is extrapolated from the midpoint of the second
# tracker read, so its error is about half that read's round trip at the
# pump's rate: 256 B covers a round trip of ~40 ms at 12 kHz. Past that, an
# estimate ahead of the pump leaves the pump that many bytes of the overtaken
# or lapped ring to play before the fill.
MIC_LEAD_REANCHOR_GUARD = 2 * REU_PUMP_CHUNK_SIZE
# The two leads of one read pair must agree this closely, else one read came
# back stale or garbled. A read that lands inside the pump's own lo/mi carry
# is off by at most 256 B and passes; it costs ~128 B of lead error.
MIC_LEAD_TORN_TOLERANCE = 1024
# A re-anchor the callback has not claimed within this many intervals is
# dropped (the first with a warning, later ones at debug), and measuring resumes: a callback that has stopped
# reaching it (every block flagged by PortAudio) must not freeze the loop.
MIC_LEAD_REANCHOR_CLAIM_INTERVALS = 3
# Consecutive failed measurements before the loop opens (drop fraction 0).
MIC_LEAD_OPEN_LOOP_AFTER = 3
# Per tracker read. requests applies it to each phase (connect, then each
# socket read), so one read can take about twice it. _measure skips its
# second read once stop() is set, so the join waits for at most one read.
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
    ) -> None:
        self._clock = clock
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
        self._pump_rate = float(sample_rate)
        self.lead_min: int | None = None
        self.lead_max: int | None = None
        self.reanchors = 0
        self.reanchors_dropped = 0
        self.open_loop_spells = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mic-lead-servo", daemon=True)
        self._thread.start()

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
                self.tick()
            except Exception:
                # A defect here must not kill the thread silently or take the
                # loop's last output with it: open the loop and say so.
                self.drop_frac = 0.0
                log.exception("audio[reu mic]: lead servo step failed; running open-loop")
                return

    def _next_wait(self) -> float:
        if not self._open_loop:
            return self._interval
        doublings = min(self._fails - MIC_LEAD_OPEN_LOOP_AFTER + 1, 16)
        ceiling = max(self._interval, MIC_LEAD_OPEN_LOOP_MAX_WAIT_S)
        return min(self._interval * 2.0**doublings, ceiling)

    def tick(self) -> None:
        """One measurement and decision. Public for the tests, which drive it
        without the thread."""
        with self._lock:
            pending = self._reanchor
            if pending is not None:
                if self._clock() - pending[1] <= MIC_LEAD_REANCHOR_CLAIM_INTERVALS * self._interval:
                    return  # the callback has not applied the last one yet
                self._reanchor = None
        if pending is not None:
            self.reanchors_dropped += 1
            (log.warning if self.reanchors_dropped == 1 else log.debug)(
                "audio[reu mic]: the mic callback has not taken a re-anchor in %.0fs; "
                "dropping it and measuring again",
                MIC_LEAD_REANCHOR_CLAIM_INTERVALS * self._interval,
            )
        m = self._measure()
        if m is None:
            if not self._stop.is_set():
                self._note_failure()
            return
        lead, pump, at = m
        self._note_success()
        last, self._last_pump = self._last_pump, (pump, at)
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
        self.lead_min = lead if self.lead_min is None else min(self.lead_min, lead)
        self.lead_max = lead if self.lead_max is None else max(self.lead_max, lead)
        if lead < 0 or lead > MIC_LEAD_REANCHOR_ABOVE:
            self.reanchors += 1
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

    def _read_pump(self) -> int | None:
        try:
            raw = self._read(REU_AUDIO_SRC_TRACKER_ADDR, 3, timeout=MIC_LEAD_READ_TIMEOUT_S)
        except Exception as e:
            # A backend that cannot read raises rather than returning None.
            log.debug("audio[reu mic]: pump-tracker read failed: %s", e)
            return None
        if raw is None or len(raw) != 3:
            return None
        src = raw[0] | (raw[1] << 8) | (raw[2] << 16)
        if not REU_MIC_BASE <= src <= REU_MIC_END:
            return None
        return (src - REU_MIC_BASE) % REU_MIC_SIZE

    def _host_between(self, before: int, after: int) -> int:
        return (before + ((after - before) % REU_MIC_SIZE) // 2) % REU_MIC_SIZE

    def _measure(self) -> tuple[int, int, float] | None:
        """Two tracker reads, each against the host position midway across
        it; a pair that disagrees means a stale or garbled read and counts as a
        failure."""
        h0 = self._write_pos()
        p1 = self._read_pump()
        h1 = self._write_pos()
        if p1 is None or self._stop.is_set():
            return None
        t1 = self._clock()
        p2 = self._read_pump()
        t2 = self._clock()
        h2 = self._write_pos()
        # p2 was sampled somewhere inside its read; the midpoint halves the
        # worst-case error of stamping it at either end.
        at = (t1 + t2) / 2
        if p2 is None:
            return None
        lead1 = signed_ring_delta(self._host_between(h0, h1), p1)
        lead2 = signed_ring_delta(self._host_between(h1, h2), p2)
        if abs(lead1 - lead2) > MIC_LEAD_TORN_TOLERANCE:
            log.debug("audio[reu mic]: torn tracker read (%+d vs %+d)", lead1, lead2)
            return None
        return (lead1 + lead2) // 2, p2, at

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
