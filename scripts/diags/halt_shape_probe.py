#!/usr/bin/env python3
"""Measure what a host DMA write costs the 6510, in NMI ticks, from the C64's side.

    scripts/diags/halt_shape_probe.py --url tr://
    scripts/diags/halt_shape_probe.py --url u64://192.168.2.64
    scripts/diags/halt_shape_probe.py --url tr:// --hold 120   # stationarity
    scripts/diags/halt_shape_probe.py --url tr:// --load-only 40 --write-rate 91
        # load generator only: occupy one link's bus while the OTHER backend
        # plays audio, to test whether a link's bus activity is what reaches
        # the SID. Measures nothing and leaves the machine alone.
    scripts/diags/halt_shape_probe.py --store-timing --nmi-rate 13636 --trials 10
        # when each sample store lands, relative to its Timer A underflow

``--store-timing`` swaps the $D418 routine for a twin that runs in exactly the
same cycles but, where the real one reads its sample, reads CIA #2 Timer B
(free-running, started on the same write as Timer A) and stores that byte into
the ring instead of $D418. The ring then holds one timestamp per serviced NMI,
read back once after the run, so the C64 itself reports each store's lateness
behind its underflow without any polling. Three outcomes are told apart: a late
store (lateness past one period, every sample still played), a lost NMI (the
lateness steps up by a whole period and stays there) and a nested NMI that
reorders or overwrites a store (a timestamp earlier than its predecessor, or a
slot never written) — in the real routine that last one plays a sample twice
and drops the next.

Every halt figure in this tree is an inference from the C64-side REC DMA model
(1 cycle/byte) or a read off the capture rig. Neither says what a *host*
``DMAWRITE`` costs, and neither can compare the two backends' halt mechanisms —
the Ultimate's internal DMA engine versus the TeensyROM's cartridge-port
DMA/BA handshake — which is the one place the two paths still differ once ring
integrity, delivery, servo control and video contention are all measured clean.

The C64 can answer this itself. The NMI consumer's read pointer R advances once
per serviced NMI, so dR/dt *is* a tick counter, and ``ring_race_probe`` phase r
established that it reads back faithfully on both backends (fits nominal to
0.01%, zero backward steps). CIA #2 is edge-triggered, so a halt spanning
several Timer A underflows latches once and the rest collapse into it: the
deficit in dR/dt under load, against the same measurement with the bus quiet, is
the halt itself, counted in lost samples by the machine that lost them.

Sweeping payload size at a **fixed write rate** separates the halt into its two
terms:

    lost_ticks_per_write  =  a  +  b * payload_bytes

``b`` is the per-byte transfer cost — 1 cycle/byte predicts b = 1/85 ticks/byte
at the 12 kHz NTSC default. ``a`` is the fixed per-write cost: bus acquisition,
arbitration, stop/resume, everything that does not scale with the payload. A
backend with a larger ``a`` steals more cycles for the same delivered bytes, and
at the worker's ~91 writes/s each extra tick of ``a`` is another 0.76% of the
sample stream held rather than updated. That is the quantity this probe exists
to compare, and it is a pure memory measurement: no capture, no ears, no
avfoundation.

Fixed write rate rather than matched byte rate because it makes ``a`` and ``b``
a straight two-parameter fit against payload, and because holding bytes constant
instead drives the small payloads past the ~200 writes/s socket-DMA ceiling,
where the link, not the halt, would set the result. ``--byte-rate`` switches to
the matched-byte variant as a cross-check when the payloads stay under that
ceiling.

The ring is static and no feeder runs: this measures the cost of a write, not
playback, so nothing here can underrun. R is sampled at the same rate in every
condition including the baseline — a read is itself a DMA on the TeensyROM,
where it shares the write link, so it has to be present in the reference or its
cost would land in ``a``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import _diaglib as d

# Bring-up is shared with ring_race_probe so both tools measure the same machine.
from ring_race_probe import arm, disarm, effective_rate, read_r, setup

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.audio.audio_handlers import (
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    NMI_STORE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
    RING_BUFFER_SIZE,
)
from c64cast.audio.audio_rate import nmi_sample_store
from c64cast.hw.backend import make_backend
from c64cast.hw.c64 import CIA2, nmi_latch_for_rate

SCRATCH_ADDR = 0x6000  # clear of the ring ($4000-$5FFF) and the NMI handler ($C020)
DEFAULT_PAYLOADS = (64, 128, 256, 512, 1024)

CIA2_TIMER_B_LO = CIA2.TIMER_A_LO + 2
CIA2_CRA = CIA2.ICR + 1
LOG_PTR_LO_ADDR = NMI_ROUTINE_ADDR + 8
LOG_PTR_HI_ADDR = NMI_ROUTINE_ADDR + 9
#: The log parks in the ring's last page instead of wrapping, so everything
#: below it is the first lap and survives until it is read back.
LOG_PARK_HI = RING_BUFFER_END_HI - 1
LOG_ENTRIES = (LOG_PARK_HI << 8) - RING_BUFFER_ADDR
LOG_FILL = 0x00
#: CIA control: start, force-load, continuous, count phi2.
CIA_START_FORCE_LOAD = 0x11
#: The STA $D418 completes four cycles after the read the twin timestamps.
READ_TO_STORE_CYCLES = 4
#: Interval window used to unwrap Timer B's low byte. Its floor admits the
#: backward step of a store reordered by a nested NMI; its top admits a lost
#: tick under a halt at the 75- and 85-cycle periods.
UNWRAP_MIN_CYCLES = -48


def store_timing_routine() -> bytes:
    """The $D418 routine with its sample read pointed at Timer B and its store
    pointed at the ring through a pointer of its own: the same opcodes and
    addressing modes, so the same cycles on every path."""
    r = bytearray(NMI_ROUTINE)
    assert (
        r[4] == 0xAD
        and r[7] == 0x8D
        and r[8:10] == bytes([NMI_STORE_ADDR & 0xFF, NMI_STORE_ADDR >> 8])
    )
    r[5:7] = bytes([CIA2_TIMER_B_LO & 0xFF, CIA2_TIMER_B_LO >> 8])
    r[8:10] = bytes([RING_BUFFER_ADDR & 0xFF, RING_BUFFER_HI])
    for at, target in (
        (11, LOG_PTR_LO_ADDR),
        (16, LOG_PTR_HI_ADDR),
        (19, LOG_PTR_HI_ADDR),
        (28, LOG_PTR_HI_ADDR),
    ):
        assert r[at - 1] in (0xEE, 0xAD, 0x8D)
        r[at : at + 2] = bytes([target & 0xFF, target >> 8])
    assert r[25] == 0xA9 and r[26] == RING_BUFFER_HI
    r[26] = LOG_PARK_HI
    return bytes(r)


def store_timing_trial(
    be, latch: int, *, payload: int, write_rate: float, secs: float
) -> bytes | None:
    """Arm the twin for one log, optionally with paced background writes, and
    read the log back once the NMI is disarmed."""
    be.write_regs(f"{CIA2.ICR:04X}", 0x7F, 0x00, 0x00)
    be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, latch >> 8, 0xFF, 0xFF)
    # Enabling the mask can fire one NMI at once for a flag left from the last
    # trial; with the timers stopped it lands before the pointer is reset.
    be.write_regs(f"{CIA2.ICR:04X}", 0x81)
    time.sleep(0.05)
    be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", bytes([LOG_FILL]) * RING_BUFFER_SIZE)
    be.write_regs(f"{LOG_PTR_LO_ADDR:04X}", RING_BUFFER_ADDR & 0xFF, RING_BUFFER_HI)
    data = bytes([0x5A]) * payload
    be.write_regs(f"{CIA2_CRA:04X}", CIA_START_FORCE_LOAD, CIA_START_FORCE_LOAD)
    t0 = time.monotonic()
    if write_rate > 0:
        nxt = t0
        while time.monotonic() - t0 < secs:
            now = time.monotonic()
            if now < nxt:
                time.sleep(nxt - now)
            nxt += 1.0 / write_rate
            be.write_memory_file(f"{SCRATCH_ADDR:04X}", data)
    else:
        time.sleep(secs)
    be.write_regs(f"{CIA2.ICR:04X}", 0x7F, 0x00, 0x00)
    raw = be.read_memory(RING_BUFFER_ADDR, LOG_ENTRIES, timeout=5.0)
    return bytes(raw) if raw is not None and len(raw) == LOG_ENTRIES else None


def _unwrap(prev: int, cur: int, lo: int) -> int:
    """The interval in cycles between two Timer B low bytes, in [lo, lo + 256)."""
    return lo + (prev - cur - lo) % 256


def analyze_store_log(log: bytes, period: int) -> dict:
    """Unwrap the Timer B bytes into absolute read times and score each store
    against the underflow its ring index belongs to.

    A slot still holding LOG_FILL is either a timestamp that happens to be
    zero or a slot a nested NMI skipped. Either way its time is unknown, so
    the unwrap steps over it; the two are told apart by the interval across
    it, which is about one period when the slot was skipped (the entry before
    it was overwritten by the nested store) and about two when it was not.
    """
    first = (0xFF - log[0]) % 256
    while first < period // 2:
        first += 256
    times: list[int | None] = [first]
    last_k, last_v, last_t = 0, log[0], first
    backward = dup_skip = 0
    for k in range(1, len(log)):
        v = log[k]
        if v == LOG_FILL and k + 1 < len(log):
            times.append(None)
            continue
        if k - last_k == 1:
            step = _unwrap(last_v, v, UNWRAP_MIN_CYCLES)
            backward += step < 0
        else:
            step = _unwrap(last_v, v, 3 * period // 2 - 128)
            dup_skip += step < 3 * period // 2
        last_t += step
        times.append(last_t)
        last_k, last_v = k, v
    store = [
        t - period * (k + 1) + READ_TO_STORE_CYCLES for k, t in enumerate(times) if t is not None
    ]
    window = 512
    mins = [min(store[i : i + window]) for i in range(0, len(store) - window + 1, window)]
    drift_periods = (mins[-1] - mins[0]) / period if mins else 0.0
    n = len(store)
    s = sorted(store)
    return {
        "entries": n,
        "store_min": s[0],
        "store_p50": s[n // 2],
        "store_p99": s[int(n * 0.99)],
        "store_max": s[-1],
        "late_frac": sum(1 for x in store if x > period) / n,
        "late2_frac": sum(1 for x in store if x > 2 * period) / n,
        "backward": backward,
        "dup_skip": dup_skip,
        "lost_ticks": round(drift_periods),
        "drift_periods": drift_periods,
    }


def store_timing(be, args) -> int:
    # A locked slice waits on the handler's $D418 store, which the twin never
    # makes, so its writes would silently fall back to WriteC64Mem.
    if getattr(be, "_lock_capable", False) is True:
        print("--store-timing cannot time locked slicing; pass --tr-slicing on or off")
        return 2
    # ceiling=1: this mode measures where the safe floor lies, so it arms
    # periods under the one production holds the timer to.
    latch = nmi_latch_for_rate(args.nmi_rate, args.system, ceiling=1)
    period = latch + 1
    fill_secs = LOG_ENTRIES * period / (1e6 * {"NTSC": 1.022727, "PAL": 0.985248}[args.system])
    secs = fill_secs * 1.3 + 0.2
    payload = args.hold_payload if args.write_rate_timing > 0 else 0
    print(
        f"\n=== store timing: {period}-cycle period, {args.trials} trials of "
        f"{LOG_ENTRIES} stores, writes {payload} B x {args.write_rate_timing:g}/s ==="
    )
    hdr = (
        f"{'trial':>5} {'min':>5} {'p50':>5} {'p99':>5} {'max':>5} {'late%':>7} "
        f"{'>2P%':>6} {'backwd':>6} {'dupskp':>6} {'lost':>5}"
    )
    print(hdr + "\n" + "-" * len(hdr))
    rows = []
    try:
        setup(be, args.system)
        be.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", store_timing_routine())
        # A TeensyROM backend slices only while a consumer is noted. No store
        # is passed: the twin never writes $D418, so a lock waiting on that
        # store would never see it.
        be.note_nmi_consumer(True)
        for trial in range(args.trials):
            log = store_timing_trial(
                be, latch, payload=payload, write_rate=args.write_rate_timing, secs=secs
            )
            if log is None:
                print(f"{trial:5d} read-back failed")
                continue
            m = analyze_store_log(log, period)
            m["log_hex"] = log.hex()
            rows.append(m)
            print(
                f"{trial:5d} {m['store_min']:5d} {m['store_p50']:5d} {m['store_p99']:5d} "
                f"{m['store_max']:5d} {m['late_frac'] * 100:6.2f}% {m['late2_frac'] * 100:5.2f}% "
                f"{m['backward']:6d} {m['dup_skip']:6d} {m['drift_periods']:5.1f}"
            )
    finally:
        be.write_regs(f"{CIA2.ICR:04X}", 0x7F, 0x00, 0x00)
        be.note_nmi_consumer(False)
        be.silence_sid()
        be.reset()
        be.close()
    if rows:
        late = [r["late_frac"] * 100 for r in rows]
        print(
            f"\nlate stores (store > {period} cycles after its underflow): "
            f"mean {sum(late) / len(late):.2f}%  min {min(late):.2f}%  max {max(late):.2f}%"
        )
        if any(abs(r["drift_periods"]) >= 0.5 for r in rows):
            print(
                "lateness is scored by ring index, so every lost tick shifts all later "
                "stores by a period: read 'lost' for those trials, not late%. A lost "
                "tick's interval past the unwrap window also aliases, which is what a "
                "negative 'lost' means."
            )
    path = d.stamped(f"storetiming_{period}", "json")
    path.write_text(
        json.dumps(
            {
                "period": period,
                "system": args.system,
                "payload": payload,
                "write_rate": args.write_rate_timing,
                "trials": rows,
            },
            indent=2,
        )
    )
    print(f"wrote {path}\nMachine silenced + reset.")
    return 0


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares ``y = a + b*x``, returning ``(a, b, r2)``."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    den = n * sxx - sx * sx
    if den == 0:
        return sy / n, 0.0, 0.0
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    ybar = sy / n
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


def _fw_cells(m: dict) -> str:
    if m["fw_lost_ticks"] is None:
        return f"{'-':>9} {'-':>7} {'-':>8} {'-':>8}"
    expected = m["fw_intervals"] + m["fw_lost_ticks"]
    pct = m["fw_lost_ticks"] / expected * 100 if expected > 0 else 0.0
    return (
        f"{pct:8.2f}% {m['fw_locked_slices']:7d} {m['fw_unlocked_slices']:8d} "
        f"{m['fw_longest_cycles']:8d}"
    )


def measure(
    be,
    secs: float,
    *,
    r_rate: float,
    eff: float,
    payload: int,
    write_rate: float,
    addr: int,
    store=None,
) -> dict:
    """Fit dR/dt while issuing ``write_rate`` background writes/sec of ``payload``
    bytes. ``write_rate = 0`` is the quiet-bus reference.

    Writes and R samples are interleaved on one thread rather than run
    concurrently: the point of comparison is the halt, and a second thread would
    add backend-lock contention that differs between the two links and would be
    indistinguishable from it in the result.
    """
    data = bytes([0x5A]) * payload  # payload entropy is irrelevant — neither link
    # escapes, compresses or RLEs the segment body
    tag = f"{addr:04X}"
    w_period = 1.0 / write_rate if write_rate > 0 else float("inf")
    r_period = 1.0 / r_rate

    if store is not None:
        d.tr_sample_watch(be, store.addr)
    t0 = time.monotonic()
    next_w = t0 + w_period
    next_r = t0
    prev_r: int | None = None
    prev_t = 0.0
    good_bytes = 0.0
    good_secs = 0.0
    backward = 0
    failed = 0
    writes = 0
    expected_step = eff * r_period

    while True:
        now = time.monotonic()
        if now - t0 >= secs:
            break
        wake = min(next_w, next_r)
        if now < wake:
            time.sleep(wake - now)

        if time.monotonic() >= next_w:
            be.write_memory_file(tag, data)
            writes += 1
            next_w += w_period

        if time.monotonic() < next_r:
            continue
        next_r += r_period
        t = time.monotonic()
        r = read_r(be)
        if r is None:
            failed += 1
            continue
        if prev_r is not None:
            step = (r - prev_r) % RING_BUFFER_SIZE
            # Same guard as phase r: a stale read reappears mod-ring as a
            # near-full-lap jump, which at this sample rate cannot be real.
            if step > 2 * expected_step:
                backward += 1
            else:
                good_bytes += step
                good_secs += t - prev_t
        prev_r, prev_t = r, t

    elapsed = time.monotonic() - t0
    fitted = good_bytes / good_secs if good_secs > 0 else 0.0
    fw = d.tr_sample_watch(be, store.addr) if store is not None else None
    return {
        "fw_lost_ticks": fw.lost_ticks(store.period_s) if fw else None,
        "fw_intervals": fw.intervals if fw else None,
        "fw_longest_cycles": fw.longest_cycles if fw else None,
        "fw_locked_slices": fw.locked_slices if fw else None,
        "fw_unlocked_slices": fw.unlocked_slices if fw else None,
        "payload": payload,
        "write_rate_req": write_rate,
        "write_rate_actual": writes / elapsed if elapsed > 0 else 0.0,
        "writes": writes,
        "byte_rate": writes * payload / elapsed if elapsed > 0 else 0.0,
        "fitted_rate_hz": fitted,
        "backward_steps": backward,
        "failed_reads": failed,
        "secs": elapsed,
    }


def load_only(be, secs: float, *, payload: int, write_rate: float, addr: int, store) -> int:
    """Occupy the bus with paced writes and do nothing else.

    The load generator for the cross-link experiment: the *other* backend plays
    audio through the production pipeline while this one writes scratch RAM at
    the worker's real rate. Every other mode in this file owns the machine —
    reset, clear loop, handler upload, arm, and a reset on the way out. This one
    must touch none of that, because a live ``c64cast`` is depending on all of
    it, so it skips the bring-up and the teardown rather than reusing
    ``measure``. It also takes no R samples: R belongs to the running app's
    consumer here, and reading it would add this link's own DMA to the very
    thing under test.
    """
    # The consumer is the other link's, but it is real, and a TeensyROM backend
    # slices only while one is noted — without this, --tr-slicing is ignored.
    be.note_nmi_consumer(True, store)
    data = bytes([0x5A]) * payload
    tag = f"{addr:04X}"
    period = 1.0 / write_rate
    print(
        f"\n=== load only: {payload} B x {write_rate:g}/s to ${addr:04X} for {secs:g}s ===\n"
        "  no reset, no handler upload, no arm, no R reads, no teardown reset"
    )
    t0 = time.monotonic()
    nxt = t0
    writes = failed = 0
    while time.monotonic() - t0 < secs:
        now = time.monotonic()
        if now < nxt:
            time.sleep(nxt - now)
        nxt += period
        try:
            be.write_memory_file(tag, data)
            writes += 1
        except Exception as exc:  # a link error must not abandon the audio run
            failed += 1
            if failed <= 3:
                print(f"  [write failed] {exc}")
    el = time.monotonic() - t0
    print(
        f"  {writes} writes in {el:.1f}s = {writes / el:.1f}/s, "
        f"{writes * payload / el / 1024:.1f} KiB/s, {failed} failed"
    )
    be.note_nmi_consumer(False)
    be.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="connection target (u64://…, tr://…)")
    ap.add_argument("--system", default="NTSC", choices=["NTSC", "PAL"])
    ap.add_argument("--nmi-rate", type=int, default=12000)
    ap.add_argument("-t", "--secs", type=float, default=20.0, help="seconds per condition")
    ap.add_argument("--r-rate", type=float, default=5.0, help="R samples/sec, every condition")
    ap.add_argument("--write-rate", type=float, default=48.0, help="background writes/sec")
    ap.add_argument(
        "--payloads",
        default=",".join(str(p) for p in DEFAULT_PAYLOADS),
        help="comma-separated payload sizes to sweep",
    )
    ap.add_argument(
        "--byte-rate",
        type=float,
        default=0.0,
        help="cross-check mode: hold total B/s here and vary the write rate instead",
    )
    ap.add_argument("--addr", type=lambda s: int(s, 16), default=SCRATCH_ADDR)
    ap.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="stationarity mode: one condition for this many seconds, reported in "
        "windows, to test whether the per-write cost drifts over a run",
    )
    ap.add_argument("--hold-payload", type=int, default=128)
    ap.add_argument("--window", type=float, default=10.0, help="--hold window length")
    ap.add_argument(
        "--load-only",
        type=float,
        default=0.0,
        help="load-generator mode: write --hold-payload bytes at --write-rate for this "
        "many seconds and measure nothing, leaving the machine untouched otherwise, so "
        "the other backend can be running a real playback at the same time",
    )
    ap.add_argument(
        "--store-timing",
        action="store_true",
        help="log when each NMI's store lands against its underflow (see the docstring)",
    )
    ap.add_argument("--trials", type=int, default=10, help="--store-timing logs to take")
    ap.add_argument(
        "--write-rate-timing",
        type=float,
        default=0.0,
        help="--store-timing background writes/sec of --hold-payload bytes (0 = quiet bus)",
    )
    d.add_tr_slicing_args(ap)
    args = ap.parse_args()

    eff = effective_rate(args.nmi_rate, args.system)
    cycles_per_tick = nmi_latch_for_rate(args.nmi_rate, args.system) + 1
    store = nmi_sample_store(cycles_per_tick - 1, args.system)
    payloads = [int(p) for p in args.payloads.split(",") if p.strip()]

    print(f"[setup] {args.url}  NMI {args.nmi_rate} -> effective {eff:.1f} Hz")
    print(
        f"[setup] {cycles_per_tick} cycles/tick; scratch ${args.addr:04X}; {args.secs:g}s/condition"
    )

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    d.apply_tr_slicing(cfg, args)
    be = make_backend(cfg)
    print(f"[setup] writes: {d.describe_tr_writes(be)}")

    if args.store_timing:
        return store_timing(be, args)

    if args.load_only > 0:
        return load_only(
            be,
            args.load_only,
            payload=args.hold_payload,
            write_rate=args.write_rate,
            addr=args.addr,
            store=store,
        )

    result: dict = {
        "url": args.url,
        "nmi_rate": args.nmi_rate,
        "effective_rate": eff,
        "cycles_per_tick": cycles_per_tick,
    }

    try:
        setup(be, args.system)
        arm(be, args.nmi_rate, args.system)
        time.sleep(1.0)
        if read_r(be) is None:
            print("[abort] R unreadable — this backend cannot report the read pointer")
            return 1

        print(f"\n=== reference: bus quiet ({args.secs:g}s, R reads only) ===")
        ref = measure(
            be,
            args.secs,
            r_rate=args.r_rate,
            eff=eff,
            payload=0,
            write_rate=0.0,
            addr=args.addr,
            store=store,
        )
        base = ref["fitted_rate_hz"]
        result["reference"] = ref
        print(
            f"  dR/dt {base:.1f} B/s vs nominal {eff:.1f} "
            f"({(base - eff) / eff * 100:+.2f}%)  backward {ref['backward_steps']}  "
            f"failed {ref['failed_reads']}"
        )
        if ref["fw_lost_ticks"] is not None:
            print(f"  firmware: {_fw_cells(ref)} (loss%, locked, unlocked, longest cycles)")
        if base <= 0:
            print("[abort] reference measured no R advance — consumer not running")
            return 1

        if args.hold > 0:
            print(
                f"\n=== hold: {args.hold_payload} B x {args.write_rate:g}/s for {args.hold:g}s ==="
            )
            hdr = f"{'window':>12} {'dR/dt':>9} {'loss%':>7} {'ticks/wr':>9} {'wr/s':>6}"
            print(hdr)
            print("-" * len(hdr))
            windows = []
            t_start = time.monotonic()
            while time.monotonic() - t_start < args.hold:
                m = measure(
                    be,
                    args.window,
                    r_rate=args.r_rate,
                    eff=eff,
                    payload=args.hold_payload,
                    write_rate=args.write_rate,
                    addr=args.addr,
                    store=store,
                )
                el = time.monotonic() - t_start
                lost = base - m["fitted_rate_hz"]
                per_w = lost / m["write_rate_actual"] if m["write_rate_actual"] > 0 else 0.0
                m["t_end"] = el
                m["lost_ticks_per_write"] = per_w
                windows.append(m)
                print(
                    f"{el:9.0f}s   {m['fitted_rate_hz']:9.1f} {lost / base * 100:6.2f}% "
                    f"{per_w:9.3f} {m['write_rate_actual']:6.1f}"
                )
            result["hold"] = windows
            if len(windows) > 1:
                pw = [w["lost_ticks_per_write"] for w in windows]
                _, slope, _ = _fit_line([w["t_end"] for w in windows], pw)
                result["hold_drift_ticks_per_write_per_min"] = slope * 60.0
                print(
                    f"\n  ticks/write {min(pw):.3f}..{max(pw):.3f}, "
                    f"drift {slope * 60:+.4f} per minute"
                )
                print(
                    "  A flat line means the halt cost is stationary, and so cannot "
                    "be what grows over a run."
                )
            path = d.stamped("haltshape_hold", "json")
            path.write_text(json.dumps(result, indent=2))
            print(f"\nwrote {path}")
            return 0

        print(f"\n=== sweep: payload vs lost ticks ({len(payloads)} conditions) ===")
        hdr = (
            f"{'payload':>8} {'wr/s':>7} {'KiB/s':>7} {'dR/dt':>9} "
            f"{'loss%':>7} {'ticks/wr':>9} {'cycles/wr':>10} {'fw loss%':>9} {'locked':>7} "
            f"{'unlocked':>8} {'longest':>8}"
        )
        print(hdr)
        print("-" * len(hdr))
        rows: list[dict] = []
        for p in payloads:
            rate = args.byte_rate / p if args.byte_rate > 0 else args.write_rate
            m = measure(
                be,
                args.secs,
                r_rate=args.r_rate,
                eff=eff,
                payload=p,
                write_rate=rate,
                addr=args.addr,
                store=store,
            )
            lost = base - m["fitted_rate_hz"]
            per_w = lost / m["write_rate_actual"] if m["write_rate_actual"] > 0 else 0.0
            m["lost_ticks_per_sec"] = lost
            m["lost_ticks_per_write"] = per_w
            m["halt_cycles_per_write"] = per_w * cycles_per_tick
            rows.append(m)
            print(
                f"{p:8d} {m['write_rate_actual']:7.1f} {m['byte_rate'] / 1024:7.1f} "
                f"{m['fitted_rate_hz']:9.1f} {lost / base * 100:6.2f}% "
                f"{per_w:9.3f} {per_w * cycles_per_tick:10.1f} {_fw_cells(m)}"
            )
        result["sweep"] = rows

        xs = [float(r["payload"]) for r in rows]
        ys = [r["lost_ticks_per_write"] for r in rows]
        a, b, r2 = _fit_line(xs, ys)
        result["fit"] = {
            "fixed_ticks_per_write": a,
            "ticks_per_byte": b,
            "r2": r2,
            "fixed_cycles_per_write": a * cycles_per_tick,
            "cycles_per_byte": b * cycles_per_tick,
        }
        print(f"\n{'=' * 62}")
        print(f"[fit] lost_ticks_per_write = {a:+.4f} + {b:.6f} * payload_bytes   (r2 {r2:.3f})")
        print(f"  fixed cost   {a * cycles_per_tick:8.1f} cycles/write  ({a:.3f} NMI ticks)")
        print(
            f"  per-byte     {b * cycles_per_tick:8.3f} cycles/byte   "
            f"(the REC DMA model predicts 1.000)"
        )
        at_worker = a + b * 128
        print(
            f"  at the worker's 128 B quantum x 91 writes/s: "
            f"{at_worker:.2f} ticks/write = {at_worker * 91 / eff * 100:.2f}% of the stream"
        )
    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        disarm(be)
        d.tr_sample_watch(be, None)
        be.silence_sid()
        be.reset()
        be.close()

    path = d.stamped("haltshape", "json")
    path.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {path}")
    print("Machine silenced + reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
