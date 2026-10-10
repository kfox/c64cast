#!/usr/bin/env python3
"""Two-SID ``$D418`` DAC feasibility probe (c64cast#590): does a second chip at
a lower mixer level, filling in between the first chip's steps, beat the
single-chip ladder?

One hardware session does the whole measurement, so every figure shares one
capture path and one set of chip temperatures:

1. **Ladders.** Each chip's 256-code signed ladder, measured with the slot ring
   ``--calibrate-dac`` uses (``dac_slot_ring``), while the other chip holds a
   constant code. The capture is AC-coupled, so a constant chip is invisible
   and each ladder comes out alone.
2. **Gain ratio + additivity.** For each fine-chip mixer level, one ring that
   alternates ``(coarse $0F, fine $00)`` with ``(coarse $00, fine $0F)`` gives
   the fine chip's level in coarse-chip units, and random ``(a, b)`` pairs in
   the same ring test whether the outputs simply add.
3. **Playback.** A tone through each candidate table — the coarse chip's
   4-bit volume nibble, its calibrated Mahoney table, and two-chip pair tables
   folded from the measured ladders — captured and scored for SNDR at several
   levels, the same analysis as ``dac_curve_playback_ab.py``.
4. **Stability.** The coarse chip's ladder again at the end, compared with
   the first.

The NMI handler here is a diag-only two-ring variant of ``NMI_ROUTINE``: ring A
at ``$4000`` drives the coarse chip's ``$D418``, ring B at ``$6000`` drives the
fine chip's, both off one read pointer.

    scripts/diags/hw_lock.py uv run scripts/diags/two_sid_dac_probe.py \\
        --url u64://HOST --pair arm2sid --gains=-18,-24,-30

``--pair arm2sid`` uses an ARM2SID's two channels (left at ``$D400`` through
``Vol Socket 1``, right at ``$D420`` through ``Vol Socket 2``); ``--pair
ultisid`` uses the two UltiSID cores. ``--replay DIR`` recomputes each candidate
table's dense bits from a saved ``results.json``. This makes sound on the real C64, and silences,
restores and resets the machine on the way out.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import _diaglib as d
import numpy as np
import sounddevice as sd

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.audio import dac_calibration as dc
from c64cast.audio import dac_pair as dp
from c64cast.audio import dac_slot_ring as dsr
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import (
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    NMI_ROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
)
from c64cast.audio.dsp import DSPParams
from c64cast.hw.backend import make_backend
from c64cast.hw.c64 import CIA2, SID, cpu_clock, nmi_latch_for_rate
from c64cast.sid import armsid
from c64cast.sid.asid_sidmap import (
    ADDR_UNMAPPED,
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_AUTO_MIRROR,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET2_EN,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID2_ADDR,
    ITEM_ULTISID_SPLIT,
)
from c64cast.sid.sid_hw_config import restore_sid_config, snapshot_sid_config
from c64cast.sid.sid_panning import CAT_MIXER
from c64cast.sid.sid_volume import VOL_ITEM, VOL_OFF, VOL_UNITY, volume_to_label

OUT = Path(__file__).resolve().parent / "out" / "two_sid"
RING_B_ADDR = 0x6000
FINE_BASE = 0xD420
TONE_CYCLES = 128
TONE_HZ = dsr.NMI_RATE * TONE_CYCLES / RING_BUFFER_SIZE

_RA_LO, _RA_HI = NMI_ROUTINE_ADDR + 5, NMI_ROUTINE_ADDR + 6
_RB_LO, _RB_HI = NMI_ROUTINE_ADDR + 11, NMI_ROUTINE_ADDR + 12
_FINE_D418 = FINE_BASE + 0x18


def _abs(addr: int) -> list[int]:
    return [addr & 0xFF, addr >> 8]


# Fast path 55 cycles including the 7-cycle NMI entry, against NMI_ROUTINE's
# 41 + 7: one more LDA abs + STA abs, and one more INC abs on the read pointer.
TWO_RING_NMI = bytes(
    [0x48]  # PHA
    + [0xAD, *_abs(0xDD0D)]  # LDA $DD0D
    + [0xAD, *_abs(RING_BUFFER_ADDR)]  # LDA RA
    + [0x8D, *_abs(0xD418)]  # STA $D418
    + [0xAD, *_abs(RING_B_ADDR)]  # LDA RB
    + [0x8D, *_abs(_FINE_D418)]  # STA fine $D418
    + [0xEE, *_abs(_RA_LO)]  # INC RA LO
    + [0xEE, *_abs(_RB_LO)]  # INC RB LO (same value, same Z)
    + [0xD0, 0x17]  # BNE done
    + [0xEE, *_abs(_RA_HI)]  # INC RA HI
    + [0xEE, *_abs(_RB_HI)]  # INC RB HI
    + [0xAD, *_abs(_RA_HI)]  # LDA RA HI
    + [0xC9, (RING_BUFFER_ADDR + RING_BUFFER_SIZE) >> 8]  # CMP #end
    + [0xD0, 0x0A]  # BNE done
    + [0xA9, RING_BUFFER_ADDR >> 8, 0x8D, *_abs(_RA_HI)]  # reset RA HI
    + [0xA9, RING_B_ADDR >> 8, 0x8D, *_abs(_RB_HI)]  # reset RB HI
    + [0x68, 0x40]  # done: PLA, RTI
)
assert TWO_RING_NMI[0x2F] == 0x68 and len(TWO_RING_NMI) == 0x31


def isolate_pair(be, pair: str) -> str:
    """Route the coarse chip to $D400 and the fine chip to $D420, alone.
    Returns the fine chip's mixer item."""
    be.put_config_item(CAT_ADDRESSING, ITEM_AUTO_MIRROR, "Disabled")
    if pair == "arm2sid":
        be.put_config_item(CAT_SOCKETS, ITEM_SOCKET1_EN, "Enabled")
        be.put_config_item(CAT_ADDRESSING, ITEM_SOCKET1_ADDR, "$D400")
        be.put_config_item(CAT_SOCKETS, ITEM_SOCKET2_EN, "Disabled")
        be.put_config_item(CAT_ADDRESSING, armsid.ITEM_EXT_SPLIT, armsid.EXT_SPLIT_RIGHT)
        be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID1_ADDR, ADDR_UNMAPPED)
        be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID2_ADDR, ADDR_UNMAPPED)
        coarse, fine = "socket1", "socket2"
    else:
        be.put_config_item(CAT_SOCKETS, ITEM_SOCKET1_EN, "Disabled")
        be.put_config_item(CAT_SOCKETS, ITEM_SOCKET2_EN, "Disabled")
        be.put_config_item(CAT_ADDRESSING, armsid.ITEM_EXT_SPLIT, armsid.EXT_SPLIT_OFF)
        be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID_SPLIT, "Off")
        be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID1_ADDR, "$D400")
        be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID2_ADDR, "$D420")
        coarse, fine = "ultisid1", "ultisid2"
    for name, item in VOL_ITEM.items():
        if name in ("socket1", "socket2", "ultisid1", "ultisid2"):
            be.put_config_item(CAT_MIXER, item, VOL_UNITY if name in (coarse, fine) else VOL_OFF)
    return VOL_ITEM[fine]


class Rig:
    def __init__(self, be, dev: int, out: Path, secs: float, settle: float) -> None:
        self.be, self.dev, self.out, self.secs, self.settle = be, dev, out, secs, settle
        self.n = 0

    def play(self, ring_a: bytes, ring_b: bytes, secs: float | None = None) -> np.ndarray:
        self.be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", ring_a)
        self.be.write_memory_file(f"{RING_B_ADDR:04X}", ring_b)
        time.sleep(self.settle)
        rec = sd.rec(
            int((secs or self.secs) * dsr.CAP_SR),
            samplerate=dsr.CAP_SR,
            channels=2,
            device=self.dev,
            dtype="float32",
        )
        sd.wait()
        mono = rec.mean(axis=1).astype(np.float64)
        np.save(self.out / f"cap{self.n:03d}.npy", mono)
        self.n += 1
        return mono

    def slot_pairs(self, pairs: list[tuple[int, int]]) -> dsr.SlotLevels:
        """Measure each (coarse, fine) pair's level against (0, 0)."""
        ra = dsr.build_slot_ring([a for a, _ in pairs], RING_BUFFER_SIZE)
        rb = dsr.build_slot_ring([b for _, b in pairs], RING_BUFFER_SIZE)
        cap = self.play(ra, rb)
        got = dsr.extract_slot_levels(cap, len(pairs), RING_BUFFER_SIZE)
        dg = got.diagnostics
        print(
            f"    ring {self.n - 1}: peak {np.abs(cap).max():.3f} "
            f"p95 spread {dg.get('pass_spread_p95_frac')} passes {dg.get('passes')}"
        )
        return got


def measure_ladder(rig: Rig, which: str, rounds: int) -> np.ndarray:
    """Return 256 levels of chip `which` ("A" coarse / "B" fine), in units of
    that chip's own $0F level."""
    plan = dsr.plan_capture_rounds(dsr.codes_per_ring(RING_BUFFER_SIZE) - 1, rounds=rounds)
    batches = []
    for rnd in plan:
        for batch in rnd:
            codes = [dsr.ANCHOR_CODE, *batch]
            pairs = [(c, 0) for c in codes] if which == "A" else [(0, c) for c in codes]
            batches.append((batch, rig.slot_pairs(pairs)))
    raw, metrics = dsr.merge_measurements(batches)
    print(f"  merge: {metrics}")
    lv = np.array([v for _, v in sorted(raw)])
    return lv / lv[dsr.ANCHOR_CODE]


def ratio_ring(rig: Rig, rng: np.random.Generator, la: np.ndarray, lb: np.ndarray):
    """One ring: the fine chip's $0F in coarse-$0F units, and random pairs
    against the additive prediction."""
    alt = [(0, 0x0F), (0x0F, 0)] * 20
    rand = [(int(a), int(b)) for a, b in rng.integers(0, 256, size=(70, 2))]
    pairs = [(0x0F, 0), *alt, *rand]
    got = rig.slot_pairs(pairs)
    lv = np.asarray(got.levels) / got.levels[0]
    a_ref = lv[1 : 1 + len(alt)][1::2]
    b_ref = lv[1 : 1 + len(alt)][0::2]
    r = float(np.median(b_ref) / np.median(a_ref))
    meas = lv[1 + len(alt) :]
    pred = np.array([la[a] + r * lb[b] for a, b in rand])
    span = float(la.max() - la.min())
    return r, meas, pred, float(np.sqrt(np.mean((meas - pred) ** 2)) / span)


def dense_bits(levels: np.ndarray) -> float:
    """ENOB of a set of achievable levels against a dense uniform target grid:
    an ideal N-level ladder scores log2(N - 1)."""
    lv = np.unique(levels)
    span = lv[-1] - lv[0]
    t = np.linspace(lv[0], lv[-1], 1 << 14)
    i = np.clip(np.searchsorted(lv, t), 1, lv.size - 1)
    near = np.where(np.abs(lv[i] - t) < np.abs(lv[i - 1] - t), lv[i], lv[i - 1])
    rms = float(np.sqrt(np.mean((near - t) ** 2)))
    return float(np.log2(span / (rms * np.sqrt(12)))) if rms else 16.0


def pair_table(la, lb, r, codes_a, codes_b, n):
    """Fold the pair ladder into n uniform targets: (codesA[n], codesB[n],
    achieved[n]), and the dense ENOB of every reachable level."""
    ca, cb = np.asarray(codes_a), np.asarray(codes_b)
    comb = (la[ca][:, None] + r * lb[cb][None, :]).ravel()
    order = np.argsort(comb)
    srt = comb[order]
    t = np.linspace(srt[0], srt[-1], n)
    i = np.clip(np.searchsorted(srt, t), 1, srt.size - 1)
    i = np.where(np.abs(srt[i] - t) < np.abs(srt[i - 1] - t), i, i - 1)
    flat = order[i]
    return ca[flat // cb.size], cb[flat % cb.size], srt[i], dense_bits(srt)


def make_tone(amp: float) -> np.ndarray:
    t = np.arange(RING_BUFFER_SIZE) / RING_BUFFER_SIZE
    return amp * np.sin(2 * np.pi * TONE_CYCLES * t)


def encode(tone: np.ndarray, ta: np.ndarray, tb: np.ndarray) -> tuple[bytes, bytes]:
    n = ta.size
    idx = np.clip(np.rint((tone + 1.0) / 2.0 * (n - 1)), 0, n - 1).astype(int)
    return ta[idx].astype(np.uint8).tobytes(), tb[idx].astype(np.uint8).tobytes()


def analyze(cap: np.ndarray, sr: int, f0: float) -> dict[str, float]:
    x = cap - cap.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freq = np.fft.rfftfreq(x.size, 1.0 / sr)
    total = float(spec[(freq > 20) & (freq < 4000)].sum())

    def band(f: float, width: float = 0.02) -> float:
        return float(spec[(freq > f * (1 - width)) & (freq < f * (1 + width))].sum())

    fund = band(f0)
    harm = sum(band(f0 * k) for k in range(2, 11))
    return {
        "sndr_db": 10 * np.log10(fund / max(total - fund, 1e-30)),
        "thd_db": 10 * np.log10(max(harm, 1e-30) / fund),
        "level": float(np.sqrt(np.mean(x**2))),
    }


def candidates(la, lb, r, n=1024) -> dict[str, tuple[np.ndarray, np.ndarray, float]]:
    vol = list(range(16))
    allc = list(range(256))
    out = {}
    lin_a = np.clip(np.rint(np.linspace(0, 15, n)), 0, 15).astype(int)
    out["A 4-bit volume"] = (lin_a, np.zeros(n, int), dense_bits(la[vol]))
    ta, _, _, bits = pair_table(la, lb, 0.0, allc, [0], n)
    out["A calibrated (1 chip)"] = (ta, np.zeros(n, int), bits)
    ta, tb, _, bits = pair_table(la, lb, r, vol, vol, n)
    out["A vol + B vol (4+4)"] = (ta, tb, bits)
    ta, tb, _, bits = pair_table(la, lb, r, allc, vol, n)
    out["A calibrated + B vol"] = (ta, tb, bits)
    ta, tb, _, bits = pair_table(la, lb, r, allc, allc, n)
    out["A calibrated + B calibrated"] = (ta, tb, bits)
    return out


def measured_pitch(cap: np.ndarray, sr: int, lo: float, hi: float) -> float:
    """Dominant frequency in [lo, hi] Hz: FFT peak + parabolic interpolation."""
    x = cap - cap.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    idx = np.where((f >= lo) & (f <= hi))[0]
    k = idx[np.argmax(spec[idx])]
    a, b, c = spec[k - 1], spec[k], spec[k + 1]
    den = a - 2 * b + c
    return float(f[k] + (0.5 * (a - c) / den if den else 0.0) * (f[1] - f[0]))


SWEEP_CYCLES = 512  # tone periods per ring: clean pitch = R / 16


def sweep(args) -> None:
    """NMI budget: play a ring-tiling tone at each rate through the one-ring and
    the two-ring handler and read the pitch. An overrunning handler queues
    NMIs, so the pitch falls below R / 16. ``--dma-load`` rewrites both rings
    with their own content in 1 KB chunks while capturing, so the 6510 sees
    the bus halts the live streaming path costs it without the audio changing."""
    import threading

    from c64cast.audio.audio_handlers import CHUNK_SIZE, NMI_ROUTINE

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)
    t = np.arange(RING_BUFFER_SIZE) * SWEEP_CYCLES / RING_BUFFER_SIZE
    tone = np.clip(np.rint(7 + 7 * np.sin(2 * np.pi * t)), 0, 15).astype(np.uint8).tobytes()
    saved: dict = {}
    try:
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        st = AudioStreamer(
            be,
            8000,
            args.system,
            dither=False,
            digi_boost=True,
            host_dma_servo=False,
            nmi_rate_adaptive=False,
            dsp_params=DSPParams(enabled=False),
        )
        st.running = True
        st._upload_nmi_and_buffers()
        saved = snapshot_sid_config(be)
        saved.update(dc._snapshot_mixer(be))
        saved.update(dc._raise_master(be))
        isolate_pair(be, args.pair)
        st._enable_digi_boost()
        be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", tone)
        be.write_memory_file(f"{RING_B_ADDR:04X}", tone)
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        dev = d.refind_sd_audio_input(args.audio)
        be.write_memory_file(f"{dp.COARSE_TABLE_ADDR:04X}", dp.IDENTITY_TABLE)
        be.write_memory_file(f"{dp.FINE_TABLE_ADDR:04X}", bytes(i & 0x0F for i in range(256)))
        handlers = (
            ("one-ring", NMI_ROUTINE),
            ("two-ring", TWO_RING_NMI),
            ("pair", dp.pair_nmi_routine(FINE_BASE)),
        )
        for handler_name, handler in handlers:
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            be.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", handler)
            base = None
            for rate in args.rates:
                latch = nmi_latch_for_rate(rate, args.system, ceiling=1)
                be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
                be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
                be.write_regs(
                    f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS
                )
                time.sleep(0.8)
                stop = threading.Event()

                def load(stop: threading.Event = stop) -> None:
                    off = 0
                    while not stop.is_set():
                        for ring in (RING_BUFFER_ADDR, RING_B_ADDR):
                            chunk = tone[off : off + CHUNK_SIZE]
                            be.write_memory_file(f"{ring + off:04X}", chunk)
                        off = (off + CHUNK_SIZE) % RING_BUFFER_SIZE
                        time.sleep(1.0 / args.dma_load_hz)

                th = threading.Thread(target=load, daemon=True) if args.dma_load else None
                if th:
                    th.start()
                try:
                    rec = sd.rec(
                        int(args.secs * dsr.CAP_SR),
                        samplerate=dsr.CAP_SR,
                        channels=2,
                        device=dev,
                        dtype="float32",
                    )
                    sd.wait()
                finally:
                    stop.set()
                    if th:
                        th.join()
                actual = cpu_clock(args.system) / (latch + 1)
                expect = actual * SWEEP_CYCLES / RING_BUFFER_SIZE
                mono = rec.mean(axis=1).astype(np.float64)
                pitch = measured_pitch(mono, dsr.CAP_SR, expect * 0.5, expect * 1.2)
                ratio = pitch / expect
                base = base or ratio
                print(
                    f"  {handler_name:8s} {rate:6d} Hz (period {latch + 1:3d} cyc)  "
                    f"pitch {pitch:8.2f} / {expect:8.2f}  rel {ratio / base - 1:+.4%}"
                )
    finally:
        try:
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            silence_fine(be)
            if saved:
                restore_sid_config(be, saved)
            be.silence_sid()
            be.reset()
            print("\n[hw] SID config + mixer restored, machine silenced + reset.")
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            print(f"[hw] cleanup warning: {e}")
        be.close()


def silence_fine(be) -> None:
    """Mute the fine chip while it is still mapped at FINE_BASE:
    ``silence_sid`` reaches $D400 only."""
    be.write_memory(f"{_FINE_D418:04X}", "00")
    for v in range(SID.N_VOICES):
        control = SID.voice_base(v) + FINE_BASE - SID.BASE + SID.OFF_CONTROL
        be.write_memory(f"{control:04X}", "00")


def arm_nmi(be, latch: int) -> None:
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
    be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)


def run(args) -> dict:
    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    results: dict = {"pair": args.pair, "gains": {}}
    saved: dict = {}
    models_back: dict[int, str] = {}
    rng = np.random.default_rng(590)
    try:
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        st = AudioStreamer(
            be,
            dsr.NMI_RATE,
            args.system,
            dither=False,
            digi_boost=False,
            dac_curve="mahoney_ultisid",
            host_dma_servo=False,
            nmi_rate_adaptive=False,
            dsp_params=DSPParams(enabled=False),
        )
        st.running = True
        st._upload_nmi_and_buffers()
        be.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", TWO_RING_NMI)
        be.write_memory_file(f"{RING_B_ADDR:04X}", bytes(RING_BUFFER_SIZE))
        latch = nmi_latch_for_rate(dsr.NMI_RATE, args.system)
        arm_nmi(be, latch)
        print("[cap] settling HDMI + re-initializing PortAudio…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        dev = d.refind_sd_audio_input(args.audio)

        saved = snapshot_sid_config(be)
        saved.update(dc._snapshot_mixer(be))
        saved.update(dc._raise_master(be))
        fine_item = isolate_pair(be, args.pair)
        for base in (0xD400, FINE_BASE):
            rep = armsid.probe(be, base)
            if args.armsid_model and rep is not None and rep.model is None:
                print(f"[hw] ${base:04X}: model unknown, not switching what cannot be restored")
            elif args.armsid_model and rep is not None and rep.model != args.armsid_model:
                models_back[base] = rep.model
                armsid.write_model(be, base, args.armsid_model)
                rep = armsid.probe(be, base)
            print(f"[hw] ${base:04X}: {rep}")
            results.setdefault("chips", {})[f"{base:04X}"] = str(rep)
        st._enable_mahoney_env()
        st._enable_mahoney_env(FINE_BASE)
        time.sleep(0.3)
        rig = Rig(be, dev, out, args.secs, args.settle)

        print("\n== ladder A (coarse) ==")
        la = measure_ladder(rig, "A", args.rounds)
        print("\n== ladder B (fine, at unity) ==")
        lb = measure_ladder(rig, "B", args.rounds)
        results["la"], results["lb"] = la.tolist(), lb.tolist()

        for g in args.gains:
            be.put_config_item(CAT_MIXER, fine_item, volume_to_label(g))
            time.sleep(0.3)
            print(f"\n== fine chip at {g} dB ==")
            r, meas, pred, add_err = ratio_ring(rig, rng, la, lb)
            print(f"  gain ratio B($0F)/A($0F) = {r:.5f}  additivity rms err {add_err:.4%} of span")
            gres = {"ratio": r, "additivity_rms_frac": add_err, "playback": {}}
            for name, (ta, tb, bits) in candidates(la, lb, r).items():
                gres["playback"][name] = {"dense_bits": round(bits, 2)}
                for amp, lab in ((0.9, "0dB"), (0.25, "-12dB"), (0.0316, "-30dB")):
                    ra, rb = encode(make_tone(amp), ta, tb)
                    m = analyze(rig.play(ra, rb), dsr.CAP_SR, TONE_HZ)
                    gres["playback"][name][lab] = {k: round(v, 3) for k, v in m.items()}
                    print(
                        f"  {name:30s} {lab:>6s} bits {bits:5.2f}  SNDR {m['sndr_db']:6.2f} dB"
                        f"  THD {m['thd_db']:7.2f}  level {m['level']:.4f}"
                    )
            ct, ft, pm = dp.fold_pair_table(la, r * lb[: len(dp.FINE_CODES)])
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            be.write_memory_file(f"{dp.COARSE_TABLE_ADDR:04X}", bytes(ct))
            be.write_memory_file(f"{dp.FINE_TABLE_ADDR:04X}", bytes(ft))
            be.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", dp.pair_nmi_routine(FINE_BASE))
            arm_nmi(be, latch)
            name = "pair routine, 8-bit index"
            gres["playback"][name] = {"metrics": pm}
            for amp, lab in ((0.9, "0dB"), (0.25, "-12dB"), (0.0316, "-30dB")):
                ra, _ = encode(make_tone(amp), np.arange(256), np.zeros(256, int))
                m = analyze(rig.play(ra, bytes(RING_BUFFER_SIZE)), dsr.CAP_SR, TONE_HZ)
                gres["playback"][name][lab] = {k: round(v, 3) for k, v in m.items()}
                print(
                    f"  {name:30s} {lab:>6s} bits {pm['ladder_bits']:5.2f}  "
                    f"SNDR {m['sndr_db']:6.2f} dB  THD {m['thd_db']:7.2f}  level {m['level']:.4f}"
                )
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            be.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", TWO_RING_NMI)
            arm_nmi(be, latch)
            results["gains"][str(g)] = gres

        be.put_config_item(CAT_MIXER, fine_item, VOL_UNITY)
        print("\n== ladder A again (stability) ==")
        la2 = measure_ladder(rig, "A", args.rounds)
        span = float(la.max() - la.min())
        results["la_repeat"] = la2.tolist()
        results["stability"] = {
            "corr": float(np.corrcoef(la, la2)[0, 1]),
            "rms_frac": float(np.sqrt(np.mean((la - la2) ** 2)) / span),
            "max_frac": float(np.max(np.abs(la - la2)) / span),
        }
        print(f"  stability: {results['stability']}")
    finally:
        try:
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            silence_fine(be)
            for base, model in models_back.items():
                armsid.write_model(be, base, model)
            if saved:
                restore_sid_config(be, saved)
            be.silence_sid()
            be.reset()
            print("\n[hw] SID config + mixer restored, machine silenced + reset.")
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            print(f"[hw] cleanup warning: {e}")
        (out / "results.json").write_text(json.dumps(results, indent=1))
        be.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url")
    ap.add_argument("--replay", type=Path)
    ap.add_argument("--pair", default="arm2sid", choices=("arm2sid", "ultisid"))
    ap.add_argument("--gains", default="-18,-24,-30", help="fine-chip mixer levels, dB")
    ap.add_argument("--system", default="NTSC", choices=("NTSC", "PAL"))
    ap.add_argument("--rounds", type=int, default=dsr.MEASURE_ROUNDS)
    ap.add_argument("--secs", type=float, default=4.5)
    ap.add_argument("--settle", type=float, default=0.4)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--rate-sweep", action="store_true", help="measure the NMI budget instead")
    ap.add_argument("--rates", default="8000,11000,12000,12500,13000,13500,14000,15000")
    ap.add_argument("--dma-load", action="store_true", help="sweep under host-DMA ring writes")
    ap.add_argument("--dma-load-hz", type=float, default=12.0)
    ap.add_argument(
        "--armsid-model",
        choices=("6581", "8580"),
        default=None,
        help="switch both ARM2SID channels to this model for the run (restored after)",
    )
    d.add_audio_device_arg(ap, "-D", "--device", dest="device", backend="sd")
    args = ap.parse_args()
    args.gains = [int(g) for g in args.gains.split(",")]
    try:
        for g in args.gains:
            volume_to_label(g)
    except ValueError as e:
        ap.error(f"--gains: {e}")
    args.rates = [int(r) for r in args.rates.split(",")]
    if args.replay:
        res = json.loads((args.replay / "results.json").read_text())
        la, lb = np.array(res["la"]), np.array(res["lb"])
        for g, gres in res["gains"].items():
            for name, (_, _, bits) in candidates(la, lb, gres["ratio"]).items():
                print(f"{g:>4s} dB  {name:30s} dense bits {bits:5.2f}")
        return 0
    if not args.url:
        ap.error("one of --url or --replay is required")
    args.audio = d.resolve_audio_input("sd", args.device)
    args.out = args.out or OUT.with_name(f"{OUT.name}-{args.pair}")
    if args.rate_sweep:
        sweep(args)
    else:
        run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
