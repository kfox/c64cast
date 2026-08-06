#!/usr/bin/env python3
"""Measure the ring race the servo is actually trying to control: where the write
head W *really* is relative to the NMI consumer's read pointer R.

    scripts/diags/ring_race_probe.py --url tr://
    scripts/diags/ring_race_probe.py --url u64://192.168.2.64 --phase r

Every other probe in this directory removes one half of the production condition.
``audio_fm_probe.py`` arms the NMI but plays a *static* ring — its background
writes never touch $4000, so it cannot see ring-content correctness at all.
``write_delivery_lag.py`` writes the ring exactly as the worker does but arms no
consumer, so there is no read pointer and no race. The one thing neither can
observe is the thing production does continuously: **the host rewriting the ring
while the player is reading it.**

That matters because the servo is a closed loop around a *single* observation —
``gap = (w_head - R) % RING_BUFFER_SIZE``, with R read back over the link. If R is
stale, filtered or biased on a given backend, the loop parks the gap at
``HOST_DMA_SERVO_TARGET_GAP`` in its own coordinates while the true margin sits
somewhere else. Far enough off and W laps R: the player reads bytes from the wrong
side of the write head, which is a discontinuity every ring lap, which is static.
Nothing in the app can currently tell — the ring write is fire-and-forget and the
only feedback is the very R that would be lying.

TWO PHASES, both backend-comparable:

  r     R readback fidelity, with the ring static and NO host writes. Sample R,
        fit dR/dt against the effective NMI rate, and count reads that go
        *backwards* (a stale or torn read-back — R is monotonic mod the ring).
        This phase alone distinguishes "R is a bad estimator" from "the race is
        real": a clean fit here means the servo's input is sound.

  race  The production condition. A feeder mirrors the audio worker's schedule
        exactly — 1024-byte chunks at a servoed pace, dripped as 128-byte quanta
        spread across the chunk period, running the real ``_servo_period`` — but
        writes **lap markers** instead of audio, so the ring's own contents say
        where the write head got to. Periodically read the whole ring back and
        compare:

          W_model  the host's own w_head (what the servo controls)
          W_true   the marker boundary: slots carrying the current lap end here
          gap_bel  (W_model - R) % SIZE   — what the servo believes
          gap_true (W_true  - R) % SIZE   — the margin the player actually has

        ``delta = gap_true - gap_bel`` is the whole point. Zero means the servo is
        controlling reality. A large or drifting delta, or a ``gap_true`` that
        approaches 0 or the ring size, is W crossing R.

Markers are one byte repeated over the quantum (``lap & 0xFF``), so a slot whose
bytes disagree is a write that landed in pieces, and a slot's lap age is readable
from a single byte. The C64 plays these as DC levels; this makes noise but is not
meant to be listened to.

Silences + resets the machine on exit.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import _diaglib as d

from c64cast.audio import (
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    HOST_DMA_SERVO_TARGET_GAP,
    READ_PTR_LO_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
    AudioStreamer,
    _servo_period,
)
from c64cast.backend import make_backend
from c64cast.c64 import CIA2, CLOCK_NTSC, CLOCK_PAL
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri
from c64cast.dsp import DSPParams

CHUNK_SIZE = 1024  # audio.py AudioStreamer.chunk_size
QUANTUM = 128  # audio.py halt_quantum_bytes at the 12 kHz NTSC default
SENTINEL = 0xFF  # prefill: "no lap has written this slot yet"


# ---- C64 bring-up ---------------------------------------------------------


def latch_for(rate: int, system: str) -> int:
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    return max(1, round(clock / rate) - 1)


def effective_rate(rate: int, system: str) -> float:
    """The rate the CIA latch grid actually yields — the consumer's real byte
    rate, and what the worker paces against."""
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    return clock / (latch_for(rate, system) + 1)


def setup(be, system: str) -> None:
    """Reset, park the machine in a running IRQ clear loop, upload the NMI
    handler, and prefill the ring with the sentinel.

    The clear loop is not optional on TeensyROM: a reset there boots into the
    cartridge menu, which is a live program that would be writing this RAM.
    """
    be.reset()
    time.sleep(1.5)
    be.run_basic_clear_loop()
    st = AudioStreamer(
        be,
        8000,  # only the worker cares, and we never start it; the latch is armed by hand
        system,
        dither=False,
        digi_boost=False,
        host_dma_servo=False,
        nmi_rate_adaptive=False,
        dsp_params=DSPParams(enabled=False),
    )
    st.running = True
    st._upload_nmi_and_buffers()
    be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", bytes([SENTINEL]) * RING_BUFFER_SIZE)


def arm(be, rate: int, system: str) -> None:
    latch = latch_for(rate, system)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
    be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)


def disarm(be) -> None:
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)


def read_r(be) -> int | None:
    """R exactly as ``AudioStreamer._read_read_ptr`` reads it — the same two bytes
    over the same channel, so a readback defect here is the servo's defect."""
    try:
        raw = be.read_memory(READ_PTR_LO_ADDR, 2)
    except Exception:
        return None
    if raw is None or len(raw) != 2:
        return None
    r = raw[0] | (raw[1] << 8)
    return r if RING_BUFFER_ADDR <= r < RING_BUFFER_END else None


# ---- phase r: is R a trustworthy observation? -----------------------------


def phase_r(be, secs: float, rate_hz: float, eff: float) -> dict:
    """Sample R with the ring static and no host writes, and fit its advance.

    Backward steps are the headline: R is the consumer's own self-modifying LDA
    operand and only ever counts up (mod the ring), so a read that comes back
    lower than its predecessor is the *link* mis-reporting, not the C64. A servo
    fed those reads is steering on noise it cannot distinguish from real drift.
    """
    period = 1.0 / rate_hz
    expected_step = eff * period
    t0 = time.monotonic()
    deadline = t0
    prev_r: int | None = None
    prev_t = 0.0
    good_bytes = 0.0
    good_secs = 0.0
    steps: list[float] = []
    backward = 0
    failed = 0
    n = 0

    while time.monotonic() - t0 < secs:
        now = time.monotonic()
        if now < deadline:
            time.sleep(deadline - now)
        deadline += period
        t = time.monotonic()
        r = read_r(be)
        if r is None:
            failed += 1
            continue
        n += 1
        if prev_r is not None:
            step = (r - prev_r) % RING_BUFFER_SIZE
            dt = t - prev_t
            # A stale/torn read that comes back behind its predecessor shows up
            # mod-ring as a near-full-lap forward jump; at this sample rate a real
            # step is a few hundred bytes, so anything past 2x expected is the
            # read, not the consumer.
            if step > 2 * expected_step:
                backward += 1
            else:
                good_bytes += step
                good_secs += dt
                steps.append(step / dt)
        prev_r, prev_t = r, t

    fitted = good_bytes / good_secs if good_secs > 0 else 0.0
    spread = 0.0
    if len(steps) > 1:
        mean = sum(steps) / len(steps)
        spread = (sum((s - mean) ** 2 for s in steps) / (len(steps) - 1)) ** 0.5
    return {
        "samples": n,
        "failed_reads": failed,
        "backward_steps": backward,
        "fitted_rate_hz": fitted,
        "expected_rate_hz": eff,
        "error_pct": (fitted - eff) / eff * 100.0 if eff else 0.0,
        "step_rate_std_hz": spread,
    }


# ---- phase race: the feeder ------------------------------------------------


class MarkerFeeder:
    """The audio worker's ring-write schedule, writing lap markers.

    Mirrors ``AudioStreamer._drip_chunk`` + ``_next_pace_increment``: quanta
    spread at the *nominal* chunk period (the servo moves the chunk boundary, not
    the intra-chunk spacing), absolute pacing, and the real ``_servo_period`` on
    the real ``(w_head - R) % RING_BUFFER_SIZE``. It imports that private
    controller deliberately — a reimplementation here would be measuring a
    different loop from the one that ships.
    """

    def __init__(self, be, eff: float, *, quantum: int, chunk: int, servo: bool):
        self.be = be
        self.chunk_period = chunk / eff
        self.quantum = quantum
        self.slots = max(1, chunk // quantum)
        self.slot_period = self.chunk_period / self.slots
        self.servo = servo
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Snapshot state, read by the sampler on the main thread.
        self.lap = 0
        self.w_head = RING_BUFFER_ADDR
        # Telemetry, matching the worker's own health line.
        self.slots_total = 0
        self.slots_late = 0
        self.late_worst_s = 0.0
        self.r_fail = 0
        self.gap_min = -1
        self.gap_max = -1

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.lap, self.w_head

    def _run(self) -> None:
        addr = RING_BUFFER_ADDR
        lap = 0
        integ = 0.0
        next_write = time.monotonic()
        while not self._stop.is_set():
            base = next_write
            for i in range(self.slots):
                sleep_s = base + i * self.slot_period - time.monotonic()
                self.slots_total += 1
                if sleep_s > 0:
                    time.sleep(sleep_s)
                else:
                    self.slots_late += 1
                    self.late_worst_s = max(self.late_worst_s, -sleep_s)
                self.be.write_memory_file(f"{addr:04X}", bytes([lap & 0xFF]) * self.quantum)
                addr += self.quantum
                if addr >= RING_BUFFER_END:
                    addr = RING_BUFFER_ADDR
                    lap = (lap + 1) & 0xFF
                with self._lock:
                    self.w_head = addr
                    self.lap = lap
                if self._stop.is_set():
                    break

            increment = self.chunk_period
            if self.servo:
                r = read_r(self.be)
                if r is None:
                    self.r_fail += 1
                else:
                    gap = (self.w_head - r) % RING_BUFFER_SIZE
                    self.gap_min = gap if self.gap_min < 0 else min(self.gap_min, gap)
                    self.gap_max = max(self.gap_max, gap)
                    increment, integ = _servo_period(gap, integ, chunk_period=self.chunk_period)
            next_write += increment

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="race-feeder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)


# ---- phase race: the sampler ----------------------------------------------


def locate_true_head(buf: bytes, lap: int, quantum: int) -> tuple[int, int, int]:
    """Decode the ring's marker map into ``(w_true_slot, torn, out_of_order)``.

    The feeder writes slots in order and bumps the lap at the wrap, and the ring
    is a whole number of quanta, so during lap L slots ``[0, w)`` carry L and
    ``[w, n)`` carry L-1. The head is therefore the length of the leading run of
    current-lap slots; anything that breaks that prefix shape is delivery landing
    out of order, which is reported rather than smoothed over.
    """
    nslots = len(buf) // quantum
    torn = 0
    ages: list[int] = []
    for s in range(nslots):
        piece = buf[s * quantum : (s + 1) * quantum]
        if len(set(piece)) > 1:
            torn += 1
        ages.append((lap - piece[0]) & 0xFF)

    w = 0
    while w < nslots and ages[w] == 0:
        w += 1
    out_of_order = sum(1 for a in ages[w:] if a == 0)
    return w, torn, out_of_order


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="connection target (u64://…, tr://…)")
    ap.add_argument("--system", default="NTSC", choices=["NTSC", "PAL"])
    ap.add_argument("--nmi-rate", type=int, default=12000)
    ap.add_argument("--phase", default="both", choices=["both", "r", "race"])
    ap.add_argument("--r-secs", type=float, default=15.0, help="phase r duration")
    ap.add_argument("--r-rate", type=float, default=5.0, help="phase r R-samples/sec")
    ap.add_argument("-t", "--secs", type=float, default=30.0, help="phase race duration")
    ap.add_argument("--sample-every", type=float, default=2.0, help="seconds between ring reads")
    ap.add_argument("--settle", type=float, default=2.0, help="feeder warm-up before sampling")
    ap.add_argument("--quantum", type=int, default=QUANTUM)
    ap.add_argument("--chunk", type=int, default=CHUNK_SIZE)
    ap.add_argument("--no-servo", action="store_true", help="feed open-loop (servo off)")
    args = ap.parse_args()

    eff = effective_rate(args.nmi_rate, args.system)
    nslots = RING_BUFFER_SIZE // args.quantum
    print(f"[setup] {args.url}  NMI {args.nmi_rate} -> effective {eff:.1f} Hz")
    print(
        f"[setup] ring {RING_BUFFER_SIZE} B = {nslots} slots x {args.quantum} B; "
        f"chunk {args.chunk} B; target gap {HOST_DMA_SERVO_TARGET_GAP}"
    )

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    result: dict = {"url": args.url, "nmi_rate": args.nmi_rate, "effective_rate": eff}
    rows: list[dict] = []
    feeder: MarkerFeeder | None = None

    try:
        setup(be, args.system)
        arm(be, args.nmi_rate, args.system)
        time.sleep(1.0)

        if read_r(be) is None:
            print("[abort] R unreadable — this backend cannot report the read pointer")
            return 1

        if args.phase in ("both", "r"):
            print(f"\n=== phase r: R readback fidelity ({args.r_secs:g}s, no host writes) ===")
            rstat = phase_r(be, args.r_secs, args.r_rate, eff)
            result["phase_r"] = rstat
            print(
                f"  fitted dR/dt {rstat['fitted_rate_hz']:.1f} B/s vs "
                f"expected {eff:.1f} ({rstat['error_pct']:+.2f}%)"
            )
            print(
                f"  samples {rstat['samples']}  failed reads {rstat['failed_reads']}  "
                f"backward steps {rstat['backward_steps']}  "
                f"step-rate std {rstat['step_rate_std_hz']:.0f} B/s"
            )

        if args.phase in ("both", "race"):
            print(f"\n=== phase race: W vs R under a live feeder ({args.secs:g}s) ===")
            feeder = MarkerFeeder(
                be, eff, quantum=args.quantum, chunk=args.chunk, servo=not args.no_servo
            )
            feeder.start()
            time.sleep(args.settle)

            hdr = (
                f"{'t':>6} {'R':>6} {'W_mod':>6} {'W_true':>6} {'gap_bel':>8} "
                f"{'gap_true':>9} {'delta':>7} {'smear':>6} {'torn':>5} {'ooo':>4} {'rd_ms':>6}"
            )
            print(hdr)
            print("-" * len(hdr))

            t0 = time.monotonic()
            next_sample = t0
            while time.monotonic() - t0 < args.secs:
                now = time.monotonic()
                if now < next_sample:
                    time.sleep(next_sample - now)
                next_sample += args.sample_every

                r0 = read_r(be)
                lap0, w0 = feeder.snapshot()
                tr0 = time.monotonic()
                buf = be.read_memory(RING_BUFFER_ADDR, RING_BUFFER_SIZE)
                read_ms = (time.monotonic() - tr0) * 1000.0
                _, w1 = feeder.snapshot()
                if r0 is None or buf is None or len(buf) != RING_BUFFER_SIZE:
                    print(f"{time.monotonic() - t0:6.1f}   read failed")
                    continue

                w_slot, torn, ooo = locate_true_head(buf, lap0, args.quantum)
                w_true = RING_BUFFER_ADDR + w_slot * args.quantum
                gap_bel = (w0 - r0) % RING_BUFFER_SIZE
                gap_true = (w_true - r0) % RING_BUFFER_SIZE
                # The feeder keeps writing during the ring read, so W_true is only
                # resolved to the span the head covered — report it rather than
                # pretending the snapshot was instantaneous.
                smear = (w1 - w0) % RING_BUFFER_SIZE
                t = time.monotonic() - t0
                rows.append(
                    {
                        "t": t,
                        "r": r0,
                        "w_model": w0,
                        "w_true": w_true,
                        "gap_believed": gap_bel,
                        "gap_true": gap_true,
                        "delta": gap_true - gap_bel,
                        "smear": smear,
                        "torn": torn,
                        "out_of_order": ooo,
                        "read_ms": read_ms,
                    }
                )
                print(
                    f"{t:6.1f} ${r0:04X} ${w0:04X} ${w_true:04X} {gap_bel:8d} "
                    f"{gap_true:9d} {gap_true - gap_bel:+7d} {smear:6d} "
                    f"{torn:5d} {ooo:4d} {read_ms:6.1f}"
                )
    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        if feeder:
            feeder.stop()
        disarm(be)
        be.silence_sid()
        be.reset()
        be.close()

    if rows:
        deltas = [r["delta"] for r in rows]
        gts = [r["gap_true"] for r in rows]
        assert feeder is not None
        result["phase_race"] = {
            "rows": rows,
            "slots_total": feeder.slots_total,
            "slots_late": feeder.slots_late,
            "late_worst_ms": feeder.late_worst_s * 1000.0,
            "servo_gap_min": feeder.gap_min,
            "servo_gap_max": feeder.gap_max,
            "r_read_failures": feeder.r_fail,
        }
        print(f"\n{'=' * 74}")
        print(f"[summary] {args.url}  {len(rows)} ring reads over {args.secs:g}s")
        print(
            f"  delta (true-believed)  mean {sum(deltas) / len(deltas):+.0f} B  "
            f"min {min(deltas):+d}  max {max(deltas):+d}"
        )
        print(
            f"  gap_true               mean {sum(gts) / len(gts):.0f} B  "
            f"min {min(gts)}  max {max(gts)}   (target {HOST_DMA_SERVO_TARGET_GAP})"
        )
        print(
            f"  servo's own gap        {feeder.gap_min}..{feeder.gap_max}  "
            f"R read failures {feeder.r_fail}"
        )
        print(
            f"  feeder slots           {feeder.slots_total} "
            f"({feeder.slots_late} late, worst +{feeder.late_worst_s * 1000:.1f} ms)"
        )
        print(
            f"  torn slots {sum(r['torn'] for r in rows)}   out-of-order {sum(r['out_of_order'] for r in rows)}"
        )
        print(
            "\n  delta near 0 means the servo is controlling the real margin. "
            "gap_true approaching 0 or "
            f"{RING_BUFFER_SIZE} is W crossing R — the player reading the wrong "
            "side of the write head."
        )

    path = d.stamped("ringrace", "json")
    path.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {path}")
    print("Machine silenced + reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
