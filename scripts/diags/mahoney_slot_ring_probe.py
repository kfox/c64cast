#!/usr/bin/env python3
"""Slot-ring probe for the Mahoney ``$D418`` calibration primitive — capture the
raw waveform once, then iterate on the extraction offline.

WHY THIS EXISTS. ``--calibrate-dac`` measures each code's *signed* output level
by filling the NMI ring with ``[code][ref]`` slot pairs behind a sync gap and
reading levels straight off one capture (see ``c64cast/dac_calibration.py``).
Everything downstream of the capture is pure — grid alignment, AC-coupling
restoration, plateau means — and alignment is the part that is easy to get
subtly, stably wrong. So this probe **saves the captured audio to .npy** and can
re-run the whole extraction against those files with no hardware attached:

    scripts/diags/mahoney_slot_ring_probe.py --url u64://HOST --source socket1
    scripts/diags/mahoney_slot_ring_probe.py --url u64://HOST --source ultisid1
    scripts/diags/mahoney_slot_ring_probe.py --replay scripts/diags/out/slotring

The hardware pass isolates one SID *source* to $D400, brings up the NMI DAC +
Mahoney environment, captures one ring per code batch, restores the SID config,
and silences + resets the machine on the way out (including on error).

``--source ultisid1`` is how the shipped ``MAHONEY_ULTISID`` table gets
re-derived. ``--calibrate-dac`` deliberately measures physical sockets only —
the emulated core's curve is claimed deterministic across units, so it is baked
rather than measured per user — which leaves no production path that can put an
UltiSID core alone on $D400. Without that, a board with a populated socket
mapped there measures the *socket* no matter which source you meant, and the
result is indistinguishable from a valid one.

This makes sound on the real C64.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from c64cast import dac_calibration as dc
from c64cast.asid_sidmap import (
    ADDR_UNMAPPED,
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_AUTO_MIRROR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET2_EN,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID2_ADDR,
)
from c64cast.audio import AudioStreamer
from c64cast.audio_handlers import (
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
)
from c64cast.backend import make_backend
from c64cast.c64 import CIA2, CLOCK_NTSC, CLOCK_PAL
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri
from c64cast.dsp import DSPParams
from c64cast.sid_hw_config import restore_sid_config, snapshot_sid_config

OUT = Path(__file__).resolve().parent / "out" / "slotring"
SOURCES = ("socket1", "socket2", "ultisid1", "ultisid2")


def isolate_ultisid(be, core: int) -> None:
    """Route UltiSID core `core` to $D400 and silence everything else that
    could answer there. The sibling of ``dc._isolate_socket``, which lives in
    the package because ``run_calibration`` needs it; this one stays here
    because nothing shipped measures an UltiSID core.

    Disabling both sockets is what makes the measurement trustworthy: with a
    populated socket left enabled at $D400, Auto Address Mirroring hands the
    address to the physical chip and the capture silently measures that chip
    instead."""
    other = ITEM_ULTISID2_ADDR if core == 1 else ITEM_ULTISID1_ADDR
    addr_item = ITEM_ULTISID1_ADDR if core == 1 else ITEM_ULTISID2_ADDR
    be.put_config_item(CAT_ADDRESSING, ITEM_AUTO_MIRROR, "Disabled")
    be.put_config_item(CAT_SOCKETS, ITEM_SOCKET1_EN, "Disabled")
    be.put_config_item(CAT_SOCKETS, ITEM_SOCKET2_EN, "Disabled")
    be.put_config_item(CAT_ADDRESSING, other, ADDR_UNMAPPED)
    be.put_config_item(CAT_ADDRESSING, addr_item, "$D400")


def isolate_source(be, source: str) -> dict[tuple[str, str], str]:
    """Put `source` alone on $D400 and alone in the mixer. Returns the mixer
    levels to restore, which snapshot_sid_config does not cover."""
    saved = dc._snapshot_mixer(be)
    if source.startswith("socket"):
        dc._isolate_socket(be, int(source[-1]))
    else:
        isolate_ultisid(be, int(source[-1]))
    dc._isolate_mixer(be, source)
    return saved


def report(codes: list[int], levels: dc.SlotLevels) -> None:
    d = levels.diagnostics
    print(f"  diagnostics: {d}")
    anchor = levels.levels[0]
    print(f"  L(${dc.ANCHOR_CODE:02X}) anchor = {anchor:+.6f}")
    pairs = zip(codes[1:], levels.levels[1:], strict=True)
    vol0 = [(c, v) for c, v in pairs if (c & 0x0F) == 0]
    if vol0:
        worst = max(abs(v / anchor) for _, v in vol0)
        shown = ", ".join(f"${c:02X}={v / anchor:+.4f}" for c, v in vol0)
        print(f"  volume-0 codes in this ring: {shown}")
        print(f"  worst |L($h0)/L($0F)| = {worst:.4f}  (want ≪ {dc.SELFTEST_TOLERANCE})")


def merge(batches: list[tuple[list[int], dc.SlotLevels]]) -> list[tuple[int, float]]:
    raw, metrics = dc.merge_measurements(batches)
    print(f"\nanchor levels per ring: {[round(m.levels[0], 6) for _, m in batches]}")
    print(f"merge: {metrics}")
    return raw


def summarize(raw: list[tuple[int, float]]) -> None:
    table, metrics = dc.build_sidtable_from_levels(raw)
    lv = np.array([v for _, v in raw])
    print("\n--- merged 256-code ladder ---")
    for hi in range(16):
        row = " ".join(f"{lv[hi * 16 + v]:+.4f}" for v in range(16))
        print(f"  ${hi:X}0-${hi:X}F  {row}")
    print(f"\nmetrics: { {k: v for k, v in metrics.items() if k != 'volume0_selftest'} }")
    print(f"volume-0 self-test per nibble: {metrics['volume0_selftest']}")
    print(
        f"\nvolume-0 self-test worst = {metrics['volume0_selftest_worst']:.4f} "
        f"(tolerance {dc.SELFTEST_TOLERANCE}) → "
        + ("PASS, table built" if table else "FAIL, table rejected")
    )


def capture_hardware(args: argparse.Namespace) -> list[tuple[list[int], dc.SlotLevels]]:
    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)
    saved: dict[tuple[str, str], str] = {}
    rounds = dc.plan_capture_rounds(dc.codes_per_ring(RING_BUFFER_SIZE) - 1, rounds=args.rounds)
    plan = [(r, b) for r, batches in enumerate(rounds) for b in batches]
    out: list[tuple[list[int], dc.SlotLevels]] = []
    out_dir = args.out or OUT.with_name(f"{OUT.name}-{args.source}")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        print("[hw] reset + NMI DAC + Mahoney env…")
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        st = AudioStreamer(
            be,
            dc.NMI_RATE,
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
        clock = CLOCK_NTSC if args.system == "NTSC" else CLOCK_PAL
        latch = max(1, round(clock / dc.NMI_RATE) - 1)
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
        be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)
        print(f"[hw] NMI armed, latch {latch} → {clock / (latch + 1):.2f} Hz")

        print("[cap] settling HDMI + re-initializing PortAudio…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        dev = dc.find_capture_device(args.device)
        print(f"[cap] device idx {dev}: {sd.query_devices(dev)['name']}")

        saved = snapshot_sid_config(be)
        saved.update(isolate_source(be, args.source))
        # Re-park after the routing change: the env is writes to $D400-$D418,
        # so the one installed at bring-up went to whatever owned that window
        # then, not to the source just routed there.
        st._enable_mahoney_env()
        print(f"[hw] {args.source} isolated at $D400, Mahoney env re-parked")
        time.sleep(0.3)

        for n, (rnd, batch) in enumerate(plan):
            codes = [dc.ANCHOR_CODE, *batch]
            be.write_memory_file(
                f"{RING_BUFFER_ADDR:04X}", dc.build_slot_ring(codes, RING_BUFFER_SIZE)
            )
            time.sleep(args.settle)
            rec = sd.rec(
                int(args.secs * dc.CAP_SR),
                samplerate=dc.CAP_SR,
                channels=2,
                device=dev,
                dtype="float32",
            )
            sd.wait()
            mono = rec.mean(axis=1).astype(np.float64)
            np.save(out_dir / f"ring{n}.npy", mono)
            np.save(out_dir / f"ring{n}.codes.npy", np.array(codes))
            print(
                f"\n[ring {n}] rotation {rnd}, {len(codes)} codes, "
                f"peak |x| = {np.abs(mono).max():.4f}, "
                f"saved {out_dir / f'ring{n}.npy'}"
            )
            got = dc.extract_slot_levels(mono, len(codes), RING_BUFFER_SIZE)
            report(codes, got)
            out.append((batch, got))
    finally:
        try:
            if saved:
                restore_sid_config(be, saved)
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            be.silence_sid()
            be.reset()
            print("\n[hw] SID config restored, U64 silenced + reset.")
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            print(f"[hw] cleanup warning: {e}")
        be.close()
    return out


def replay(path: Path) -> list[tuple[list[int], dc.SlotLevels]]:
    out: list[tuple[list[int], dc.SlotLevels]] = []
    for n, cap_file in enumerate(sorted(path.glob("ring*[0-9].npy"))):
        mono = np.load(cap_file)
        codes = np.load(cap_file.with_suffix(".codes.npy")).tolist()
        print(f"\n[ring {n}] {cap_file.name}: {mono.size} samples, {len(codes)} codes")
        got = dc.extract_slot_levels(mono, len(codes), RING_BUFFER_SIZE)
        report(codes, got)
        out.append((codes[1:], got))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", help="connection target, e.g. u64://HOST")
    ap.add_argument("--replay", type=Path, help="re-run extraction on saved .npy captures")
    ap.add_argument(
        "--source",
        default="socket1",
        choices=SOURCES,
        help="which SID source to isolate at $D400 (default socket1)",
    )
    ap.add_argument("--system", default="NTSC", choices=("NTSC", "PAL"))
    ap.add_argument("--device", type=int, default=None, help="capture device index")
    ap.add_argument(
        "--rounds",
        type=int,
        default=dc.MEASURE_ROUNDS,
        help="how many rotations of the slot order to average (see MEASURE_ROUNDS)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"where to save captures (default {OUT.name}-<source>, so runs of "
        "different sources stay comparable instead of overwriting each other)",
    )
    ap.add_argument("--secs", type=float, default=4.5)
    ap.add_argument("--settle", type=float, default=0.4)
    args = ap.parse_args()

    if args.replay:
        batches = replay(args.replay)
    elif args.url:
        batches = capture_hardware(args)
    else:
        ap.error("one of --url or --replay is required")
    summarize(merge(batches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
