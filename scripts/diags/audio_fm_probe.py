#!/usr/bin/env python3
"""Host-DMA bus-halt perturbation probe for the 4-bit ``$D418`` DAC — CAPTURE ONLY.

Every host ``DMAWRITE`` freezes the 6510 for the duration of the transfer. CIA #2
is **edge-triggered** through the NMI line, so when one halt spans several Timer A
underflows the ICR latches once and the rest collapse into that same edge — every
NMI past the first is lost. Audibly that is two things at once: a slow drift (lost
samples ⇒ the consumer runs below nominal ⇒ the carrier sits flat) and a per-write
impulse that frequency-modulates whatever is playing.

This tool measures both, and — the reason it exists — measures how they scale with
**payload size**. That curve has only ever been sampled at 1024 and 4096 bytes,
both far *above* one NMI period, and the per-write halt duration for a host
DMAWRITE is nowhere measured in the tree (every µs/cycle halt figure in c64cast is
for C64-side REC DMA). The C64-side fix for exactly this problem is already shipped
and measured — ``modes.BANK_SWAP_CHUNK_SIZE = 100`` splits each REC DMA so its halt
stays under one NMI period, taking NMI capture from 67% to 97% — but it was never
applied to the host write path, where the audio worker still pushes 1024 bytes in
one command.

METHOD (STATIC RING + PACED BACKGROUND WRITES)
  The whole 8 KB NMI ring is filled ONCE with a sine that tiles it exactly, and
  the NMI is armed. There is no worker and no host feed, so the tone **cannot
  underrun** — an earlier attempt at this measurement ran through a production
  feed and its NEUTRAL-pad dropouts contaminated the metric. The ring then loops
  forever on its own while a background thread issues DMA writes at a controlled
  size and cadence, and we capture the result off the Cam Link.

  Three conditions:
    ref  — no background writes at all (the clean floor)
    ram  — writes to plain RAM at $2000
    io   — writes to $D800 (color RAM). The positive control: I/O writes keep the
           CPU stop even on a build patched to skip it for plain RAM.

  The sweep holds **total byte rate constant** and varies the payload, so event
  size and event count trade off against each other and the comparison is about
  halt *shape*, not bandwidth. Interpreting the carrier downshift across that
  sweep is the point: downshift roughly flat vs payload ⇒ halt time is dominated
  by the bytes, so splitting a write costs little and buys the sub-NMI-period
  quantum; downshift that climbs as the payload shrinks ⇒ a fixed per-write cost
  dominates, which is what would put a floor under how small the quantum can go.

  ``--burst N`` groups N writes back-to-back instead of spreading them evenly.
  Spread-vs-burst at identical size and byte rate is the decisive control for
  whether the firmware actually resumes the CPU between queued commands: if burst
  and spread perturb identically, the commands are being serviced as one halt and
  splitting cannot help from the host side.

METRICS
  * carrier Hz and its downshift vs ref — a direct read on total halt fraction.
  * inst-freq deviation (std, Hz) via an FFT-built analytic signal — no scipy.
  * the **modulation spectrum** of that inst-freq trace, integrated into bands.
    ``mod 4-20Hz`` is the one to watch: human sensitivity to amplitude and
    frequency modulation peaks there, and today's 1024-byte cadence (~11.7 writes/s
    at the 12 kHz default) lands squarely inside it. A change that moves the same
    modulation energy *out* of that band is a real perceptual win even when total
    modulation power is unchanged, which is exactly what a smaller quantum should
    do — and is invisible to a metric that only sums sideband power.
  * achieved writes/sec, which doubles as the sustained command-rate ceiling
    measurement (``BackendProfile.max_write_rate_hz`` is declared but unmeasured
    on TeensyROM).

Absolute *timing* off this capture is not trustworthy — avfoundation drops samples
by a load-dependent 10-23% — so everything here is either a frequency reading or a
ratio against the ref condition captured through the same chain.

This makes sound on the real C64. Silences + resets the machine on exit.

    scripts/diags/audio_fm_probe.py --url u64://192.168.2.64
    scripts/diags/audio_fm_probe.py --url tr:// --write-bytes 64,128,1024
    scripts/diags/audio_fm_probe.py --write-bytes 128 --burst 8   # coalescing check
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import _diaglib as d
import numpy as np
import sounddevice as sd

from c64cast.audio import (
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    NEUTRAL_SAMPLE,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
    AudioStreamer,
)
from c64cast.backend import make_backend
from c64cast.c64 import CIA2, CLOCK_NTSC, CLOCK_PAL
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri
from c64cast.dsp import DSPParams

CAP_SR = 48000
CAP_DEVICE = 1  # Cam Link 4K audio (sounddevice idx); resolved by name at runtime

# The ring holds exactly RING_CYCLES sine periods so it loops with no wrap
# discontinuity. 256 cycles over 8 KB = 32 samples/period; at the 12 kHz default
# that is a ~376 Hz carrier — low enough to leave clean spectrum on both sides for
# sidebands out to a couple of hundred Hz.
RING_CYCLES = 256

#: Background-write targets: (base address, span to cycle the write address over).
#: The write address advances like the audio worker's does rather than hammering
#: one address, so the firmware's per-command path sees production-shaped traffic.
#: $2000 is plain RAM here (no bitmap mode is engaged) and is clear of the ring at
#: $4000-$5FFF and the handler at $C020. $D800 is color RAM — visibly garbled
#: during an io run, which is expected and harmless.
TARGETS: dict[str, tuple[int, int]] = {
    "ram": (0x2000, 0x2000),
    "io": (0xD800, 1000),
}

#: Modulation-spectrum integration bands (Hz) over the inst-freq trace. 4-20 is
#: the perceptually loaded one — see the module docstring.
MOD_BANDS: tuple[tuple[str, float, float], ...] = (
    ("0.5-4Hz", 0.5, 4.0),
    ("4-20Hz", 4.0, 20.0),
    ("20-60Hz", 20.0, 60.0),
    ("60-250Hz", 60.0, 250.0),
)


# ---- C64 bring-up ---------------------------------------------------------


def latch_for(rate: int, system: str) -> int:
    """CIA #2 Timer A latch (period = latch+1 cycles) for `rate` — the same math
    as ``AudioStreamer._nmi_latch_value``."""
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    return max(1, round(clock / rate) - 1)


def effective_rate(rate: int, system: str) -> float:
    """The rate the CIA latch grid actually yields — the real consumer rate, and
    the byte rate the production worker paces against."""
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    return clock / (latch_for(rate, system) + 1)


def build_ring_tone(cycles: int) -> bytes:
    """The full 8 KB ring filled with `cycles` sine periods, encoded to 4-bit DAC
    codes centered on NEUTRAL_SAMPLE. Tiles exactly so the NMI loops it forever
    with no wrap glitch and no host feed."""
    samples_per_cycle = RING_BUFFER_SIZE / cycles
    t = np.arange(RING_BUFFER_SIZE) / samples_per_cycle
    codes = np.rint(NEUTRAL_SAMPLE + 7.0 * np.sin(2 * np.pi * t))
    return np.clip(codes, 0, 15).astype(np.uint8).tobytes()


def setup(be, system: str, cycles: int) -> None:
    """One-time bring-up: reset, running IRQ clear loop, upload the NMI handler,
    then overwrite the neutral ring with the tiled tone. The reset happens ONCE
    so HDMI renegotiates once, not per condition."""
    be.reset()
    time.sleep(1.5)
    be.run_basic_clear_loop()
    st = AudioStreamer(
        be,
        8000,  # only affects the worker, which we never start; the latch is armed by hand
        system,
        dither=False,
        digi_boost=True,
        host_dma_servo=False,
        nmi_rate_adaptive=False,
        dsp_params=DSPParams(enabled=True),
    )
    st.running = True
    st._upload_nmi_and_buffers()
    be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", build_ring_tone(cycles))


def arm(be, rate: int, system: str) -> None:
    """(Re)arm the NMI at `rate`: disarm, set the Timer A latch, enable."""
    latch = latch_for(rate, system)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
    be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)


def disarm(be) -> None:
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)


# ---- background writer ----------------------------------------------------


def _sleep_until(deadline: float) -> None:
    """Sleep to `deadline` with a short spin tail. Plain time.sleep resolution on
    macOS is ~1 ms, and at 188 writes/s the interval is only 5.3 ms — sleeping
    alone would smear the very cadence this tool is trying to resolve."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.0015)
        # else: spin


@dataclass
class WriterStats:
    writes: int = 0
    elapsed: float = 0.0
    late_writes: int = 0  # writes that missed their slot (the socket is saturated)

    @property
    def rate_hz(self) -> float:
        return self.writes / self.elapsed if self.elapsed > 0 else 0.0


class BackgroundWriter:
    """Issues paced DMA writes of a fixed payload size while the tone plays.

    `burst` writes are issued back-to-back at each slot; slots repeat at
    `burst * write_bytes / byte_rate`. burst=1 spreads writes evenly (the shape a
    split audio chunk would have); burst=N re-bunches the same bytes into one
    burst per slot (the shape today's single 1024-byte write has, and the control
    for whether the firmware resumes the CPU between queued commands).
    """

    def __init__(self, be, target: str, write_bytes: int, byte_rate: float, burst: int):
        self.be = be
        self.base, self.span = TARGETS[target]
        self.write_bytes = min(write_bytes, self.span)
        self.burst = max(1, burst)
        self.slot_period = (self.burst * self.write_bytes) / byte_rate
        # Deterministic payload: fixed seed so every condition pushes identical
        # bytes and nothing in the path can be reacting to content.
        rng = np.random.default_rng(0x64CA57)
        self.payload = rng.integers(0, 256, size=self.write_bytes, dtype=np.uint8).tobytes()
        self.stats = WriterStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        addr = self.base
        end = self.base + self.span
        next_slot = time.perf_counter()
        t0 = next_slot
        while not self._stop.is_set():
            _sleep_until(next_slot)
            if time.perf_counter() > next_slot + self.slot_period:
                self.stats.late_writes += 1
            for _ in range(self.burst):
                self.be.write_memory_file(f"{addr:04X}", self.payload)
                self.stats.writes += 1
                addr += self.write_bytes
                if addr + self.write_bytes > end:
                    addr = self.base
            next_slot += self.slot_period
        self.stats.elapsed = time.perf_counter() - t0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fmprobe-writer", daemon=True)
        self._thread.start()

    def stop(self) -> WriterStats:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        return self.stats


# ---- analysis -------------------------------------------------------------


def analytic_band(x: np.ndarray, sr: int, f0: float, half_width: float) -> np.ndarray:
    """Band-passed analytic signal around `f0`, built in the frequency domain.

    Done by hand rather than with scipy.signal.hilbert — scipy is not a project
    dependency, and zeroing everything outside the band while doubling the
    positive frequencies gives the band-limited analytic signal in one pass.
    """
    n = x.size
    spec = np.fft.fft(x)
    freqs = np.fft.fftfreq(n, 1.0 / sr)
    gain = np.zeros(n)
    gain[(freqs >= f0 - half_width) & (freqs <= f0 + half_width)] = 2.0
    return np.fft.ifft(spec * gain)


def carrier_hz(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    """Dominant frequency in [lo, hi] via FFT peak + parabolic interpolation."""
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft((x - x.mean()) * win))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    band = np.where((freqs >= lo) & (freqs <= hi))[0]
    k = band[np.argmax(spec[band])]
    if 0 < k < spec.size - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if denom else 0.0
    else:
        delta = 0.0
    return float(freqs[k] + delta * (freqs[1] - freqs[0]))


@dataclass
class Analysis:
    carrier: float
    peak_hz: float
    inst_freq_std: float
    mod_bands: dict[str, float] = field(default_factory=dict)
    write_cadence_peak: float = 0.0


def analyze(mono: np.ndarray, sr: int, expected: float, cadence_hz: float) -> Analysis:
    """Carrier, inst-freq deviation, and the modulation spectrum by band.

    The inst-freq trace is decimated by block-averaging before its own FFT: the
    modulation content of interest is under ~250 Hz, and averaging is both the
    anti-alias filter and the decimator.
    """
    x = mono - mono.mean()
    peak = carrier_hz(x, sr, expected * 0.6, expected * 1.4)
    # Half-width has to admit the sidebands we are trying to measure without
    # letting the neighbouring harmonic in; the carrier's own harmonic sits at
    # 2*f0, so f0/2 is the widest safe window.
    analytic = analytic_band(x, sr, peak, half_width=peak / 2)
    phase = np.unwrap(np.angle(analytic))
    inst = np.diff(phase) * sr / (2 * np.pi)

    # Trim the filter's edge transients before any statistic is taken.
    edge = int(0.25 * sr)
    inst = inst[edge:-edge] if inst.size > 3 * edge else inst

    decim = max(1, sr // 1000)
    usable = (inst.size // decim) * decim
    trace = inst[:usable].reshape(-1, decim).mean(axis=1)
    trace_sr = sr / decim

    win = np.hanning(trace.size)
    # Normalize so band powers are in Hz-deviation units and comparable across runs
    # of different length.
    spec = np.abs(np.fft.rfft((trace - trace.mean()) * win)) * (2.0 / win.sum())
    freqs = np.fft.rfftfreq(trace.size, 1.0 / trace_sr)

    bands: dict[str, float] = {}
    for name, lo, hi in MOD_BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        bands[name] = float(np.sqrt(np.sum(spec[sel] ** 2))) if sel.any() else 0.0

    cadence_peak = 0.0
    if cadence_hz > 0:
        near = (freqs >= cadence_hz * 0.8) & (freqs <= cadence_hz * 1.2)
        if near.any():
            cadence_peak = float(spec[near].max())

    # Report the MEAN of the instantaneous frequency, not the FFT peak. Frequency
    # modulation is zero-mean, so the inst-freq mean is the carrier by
    # construction — whereas the FFT peak jumps to a sideband once the modulation
    # index passes ~1.4 (J1 overtakes J0), which would read as a spurious carrier
    # shift of exactly one modulation frequency. The peak is still what locates
    # the band above; it is not what gets reported.
    return Analysis(
        carrier=float(np.mean(inst)),
        peak_hz=peak,
        inst_freq_std=float(np.std(inst)),
        mod_bands=bands,
        write_cadence_peak=cadence_peak,
    )


# ---- capture --------------------------------------------------------------


def find_camlink(fallback: int) -> int:
    """Resolve the Cam Link audio index by NAME — PortAudio re-enumerates after
    the HDMI hotplug a reset causes, so a remembered index goes stale."""
    for i, dev in enumerate(sd.query_devices()):
        if "cam link" in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    return fallback


def write_wav(path: Path, mono: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(mono * 32767, -32768, 32767).astype(np.int16).tobytes())


def run_condition(
    be,
    label: str,
    target: str | None,
    write_bytes: int,
    byte_rate: float,
    burst: int,
    secs: float,
    device: int,
    expected: float,
) -> tuple[Analysis, WriterStats]:
    """One capture: optionally start the background writer, record, analyze."""
    writer = None
    cadence = 0.0
    if target is not None:
        writer = BackgroundWriter(be, target, write_bytes, byte_rate, burst)
        cadence = 1.0 / writer.slot_period
        writer.start()
        time.sleep(0.5)  # let the write cadence settle before recording

    rec = sd.rec(int(secs * CAP_SR), samplerate=CAP_SR, channels=2, device=device, dtype="float32")
    sd.wait()
    stats = writer.stop() if writer else WriterStats()

    mono = rec.mean(axis=1).astype(np.float64)
    write_wav(d.out_dir() / f"fmprobe_{label}.wav", mono, CAP_SR)
    return analyze(mono, CAP_SR, expected, cadence), stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="u64://192.168.2.64", help="connection URI")
    ap.add_argument("--system", default="NTSC", choices=["NTSC", "PAL"])
    ap.add_argument("--nmi-rate", type=int, default=12000, help="DAC sample rate (default 12000)")
    ap.add_argument(
        "--write-bytes",
        default="1024,256,128,80,64,32",
        help="payload sizes to sweep, at matched total byte rate",
    )
    ap.add_argument(
        "--targets", default="ram,io", help="background-write targets: ram, io (ref always runs)"
    )
    ap.add_argument(
        "--burst",
        type=int,
        default=1,
        help="writes issued back-to-back per slot (1 = spread evenly)",
    )
    ap.add_argument(
        "--byte-rate",
        type=float,
        default=0.0,
        help="background bytes/sec (default: the DAC's own consumption rate)",
    )
    ap.add_argument("--secs", type=float, default=8.0, help="capture seconds per condition")
    ap.add_argument("--ring-cycles", type=int, default=RING_CYCLES)
    ap.add_argument("--device", type=int, default=CAP_DEVICE, help="Cam Link audio sd index")
    args = ap.parse_args()

    sizes = [int(s) for s in args.write_bytes.split(",")]
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        ap.error(f"unknown target(s) {unknown}; known: {sorted(TARGETS)}")

    eff = effective_rate(args.nmi_rate, args.system)
    byte_rate = args.byte_rate or eff
    expected = args.ring_cycles * eff / RING_BUFFER_SIZE
    nmi_period_us = 1e6 / eff

    print(f"NMI {args.nmi_rate} Hz -> effective {eff:.1f} Hz, period {nmi_period_us:.1f} us")
    print(f"carrier expected ~{expected:.1f} Hz; background byte rate {byte_rate:.0f} B/s")
    print(f"payload sizes: {sizes}  targets: {targets}  burst: {args.burst}")

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    rows: list[dict] = []
    try:
        setup(be, args.system, args.ring_cycles)
        arm(be, args.nmi_rate, args.system)
        print("[cap] settling HDMI + re-initializing PortAudio…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        device = find_camlink(args.device)
        print(f"[cap] capturing from idx {device}: {sd.query_devices(device)['name']}")

        print("\n=== ref (no background writes) ===")
        ref, _ = run_condition(be, "ref", None, 0, byte_rate, 1, args.secs, device, expected)
        rows.append({"cond": "ref", "target": "-", "bytes": 0, "analysis": ref, "stats": None})
        print(f"    carrier {ref.carrier:.2f} Hz  inst-freq std {ref.inst_freq_std:.1f} Hz")

        for target in targets:
            for size in sizes:
                label = f"{target}_{size}"
                slot_hz = byte_rate / (size * args.burst)
                print(f"\n=== {target} {size} B x{args.burst}  ({slot_hz:.1f} slots/s) ===")
                analysis, stats = run_condition(
                    be,
                    label,
                    target,
                    size,
                    byte_rate,
                    args.burst,
                    args.secs,
                    device,
                    expected,
                )
                rows.append(
                    {
                        "cond": label,
                        "target": target,
                        "bytes": size,
                        "analysis": analysis,
                        "stats": stats,
                    }
                )
                print(
                    f"    carrier {analysis.carrier:.2f} Hz "
                    f"(down {ref.carrier - analysis.carrier:+.2f})  "
                    f"std {analysis.inst_freq_std:.1f} Hz  "
                    f"writes {stats.rate_hz:.1f}/s (late {stats.late_writes})"
                )
                time.sleep(0.3)
    finally:
        disarm(be)
        be.silence_sid()
        be.reset()
        be.close()

    # ---- report ----
    band_names = [b[0] for b in MOD_BANDS]
    header = (
        f"{'condition':>12} {'carrier':>9} {'down':>7} {'std':>7} "
        + " ".join(f"{n:>9}" for n in band_names)
        + f" {'wr/s':>7} {'halt_us':>8}"
    )
    print("\n" + header)
    print("-" * len(header))
    for row in rows:
        a: Analysis = row["analysis"]
        st: WriterStats | None = row["stats"]
        down = ref.carrier - a.carrier
        # Halt fraction from the carrier downshift: the consumer loses that
        # fraction of its ticks, so per-write halt = fraction / writes-per-second.
        halt_us = ""
        if st and st.rate_hz > 0 and ref.carrier > 0:
            frac = down / ref.carrier
            halt_us = f"{frac / st.rate_hz * 1e6:8.1f}" if frac > 0 else f"{0.0:8.1f}"
        print(
            f"{row['cond']:>12} {a.carrier:>9.2f} {down:>+7.2f} {a.inst_freq_std:>7.1f} "
            + " ".join(f"{a.mod_bands[n]:>9.2f}" for n in band_names)
            + f" {st.rate_hz if st else 0.0:>7.1f} {halt_us:>8}"
        )

    print(
        f"\nNMI period is {nmi_period_us:.1f} us. A payload whose implied halt_us "
        f"lands under that cannot swallow a second NMI underflow, so its tick loss "
        f"should collapse and its modulation should move out of the 4-20Hz band."
    )
    print(
        "halt_us is inferred from the carrier downshift and assumes the downshift "
        "is entirely lost ticks — treat it as an upper bound, and cross-check the "
        "trend against hostdma_drift_probe.py's R_rate before trusting a value."
    )

    dump = {
        "url": args.url,
        "system": args.system,
        "nmi_rate": args.nmi_rate,
        "effective_rate": eff,
        "nmi_period_us": nmi_period_us,
        "byte_rate": byte_rate,
        "burst": args.burst,
        "carrier_expected": expected,
        "rows": [
            {
                "cond": r["cond"],
                "target": r["target"],
                "bytes": r["bytes"],
                "carrier": r["analysis"].carrier,
                "inst_freq_std": r["analysis"].inst_freq_std,
                "mod_bands": r["analysis"].mod_bands,
                "write_cadence_peak": r["analysis"].write_cadence_peak,
                "peak_hz": r["analysis"].peak_hz,
                "writes_hz": r["stats"].rate_hz if r["stats"] else None,
                "late_writes": r["stats"].late_writes if r["stats"] else None,
            }
            for r in rows
        ],
    }
    path = d.stamped("fmprobe", "json")
    path.write_text(json.dumps(dump, indent=2))
    print(f"\nwrote {path}")
    print("Machine silenced + reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
