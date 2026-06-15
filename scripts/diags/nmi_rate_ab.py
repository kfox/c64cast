#!/usr/bin/env python3
"""Hardware A/B of the NMI audio SAMPLE RATE on the U64 (host-DMA path).

The 4-bit `$D418` DAC streams at 8 kHz → a 4 kHz Nyquist that lops off the
fricative/sibilant band (the "gra-ss"→no-ss intelligibility loss). 16 kHz
overruns the NMI handler under VIC badlines (audio.py docstring). The open
question this tool answers: is there a SAFE intermediate rate (~10-11 kHz)
that recovers usable high-band energy WITHOUT overrunning the handler — and
how does the ceiling differ PAL vs NTSC (PAL's slower clock = fewer cycles
per NMI period = less badline headroom)?

For each clip it plays two passes through the production host-DMA path
(decode → peak-normalize → DSP chain → 4-bit encode), one at --rate-a, one at
--rate-b, captures each off the Cam Link, and reports:

  1. EFFECTIVE consumer rate R (the overrun ceiling, MEASURED on real silicon)
     — polls the NMI read pointer ($C025/$C026) during playback and linear-fits
     bytes/sec. If R tracks the configured rate the handler keeps up; if R
     plateaus below it, that rate overran on THIS standard. This turns the
     cycle-budget table into a per-standard empirical ceiling.
  2. TWO-DOMAIN capture analysis (per the standing "spectra mislead, ears
     decide — but look at BOTH" rule): time-domain RMS/crest + amplitude
     envelope, AND the 4-5.5 kHz captured-band energy (the direct evidence the
     higher rate actually reproduces the recovered band). A PNG overlays
     waveform-envelope + spectrogram for A vs B.

METHODOLOGY: the A/B needs WIDEBAND source. `decode_audio_full(clip, rate)`
resamples (anti-aliased) per pass, so rate-a is correctly band-limited and
rate-b keeps its extra octave — that delta IS the experiment. The pre-band-
limited 8 kHz OSR clips can't show it; feed full-band speech/music.

--system MUST match the U64's actual System Mode (it sets the latch math). With
--switch-system the tool sets System Mode over REST + reboots + waits for the
unit, so a PAL run really runs PAL timing. It restores nothing automatically —
the caller restores System Mode → NTSC at end of session.

    scripts/diags/nmi_rate_ab.py --system NTSC --rate-b 10500 --switch-system \\
        assets/audio/KevinMacLeod_Carefree.mp3 assets/audio/clean_speech.wav

This needs your EARS — it makes sound on the real U64.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import wave
from pathlib import Path

import _diaglib as d
import numpy as np
import sounddevice as sd

from c64cast.api import Ultimate64API
from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
    AudioStreamer,
)
from c64cast.dsp import DSPParams
from c64cast.video import _compute_normalization_gain, decode_audio_full

CAP_SR = 48000
CAP_DEVICE = 1  # Cam Link 4K audio (sounddevice idx); override with --device
READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 (LO)/$C026 (HI) — NMI read pointer
SYS_CATEGORY = "U64 Specific Settings"
SYS_SETTING = "System Mode"

# Recovered high band: above the 8 kHz Nyquist (4 kHz), up to the ~5.25-5.5 kHz
# Nyquist of a 10.5-11 kHz pass. Energy here in the B capture but not the A
# capture is the direct proof the higher rate reproduces new content.
HI_BAND = (4000.0, 5500.0)


def save_wav(path: str, mono: np.ndarray, sr: int) -> None:
    pcm = np.clip(mono * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# --- effective-R probe (overrun ceiling) ---------------------------------
class RProbe:
    """Background poller of the NMI read pointer → measured consumer rate R.

    Same unwrap-and-linfit math as hostdma_drift_probe.py, but in-thread so it
    runs alongside the in-process AudioStreamer feed (REST reads don't contend
    with the DMA socket)."""

    def __init__(self, url: str, hz: float = 50.0):
        self.url = url
        self.period = 1.0 / hz
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.ts: list[float] = []
        self.cum: list[float] = []
        self.missed = 0

    def _read_r(self) -> int | None:
        b = d.rest_readmem(READ_PTR_ADDR, 2, self.url)
        if not b or len(b) != 2:
            return None
        addr = b[0] | (b[1] << 8)
        return addr if RING_BUFFER_ADDR <= addr < RING_BUFFER_END else None

    def _run(self) -> None:
        t0 = time.time()
        nxt = t0
        prev: int | None = None
        cum = 0
        while not self._stop.is_set():
            now = time.time()
            if now < nxt:
                time.sleep(min(nxt - now, self.period))
            nxt += self.period
            r = self._read_r()
            if r is None:
                self.missed += 1
                continue
            if prev is not None:
                delta = (r - prev) % RING_BUFFER_SIZE
                if delta >= RING_BUFFER_SIZE // 2:  # backward tear → ignore
                    delta = 0
                cum += delta
            prev = r
            self.ts.append(now - t0)
            self.cum.append(float(cum))

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=2.0)

    def r_rate(self) -> float | None:
        if len(self.ts) < 10:
            return None
        ts, ys = self.ts, self.cum
        n = len(ts)
        mt, my = sum(ts) / n, sum(ys) / n
        sxx = sum((t - mt) ** 2 for t in ts)
        sxy = sum((t - mt) * (y - my) for t, y in zip(ts, ys, strict=False))
        return sxy / sxx if sxx else None


def play_one(
    url: str,
    clip: str,
    rate: int,
    system: str,
    secs: float,
    device: int,
    label: str,
) -> tuple[str, float | None, int]:
    """Play `clip` resampled to `rate` through the host-DMA path; capture +
    probe R. Returns (wav_path, measured_R_rate, configured_rate)."""
    int16 = decode_audio_full(clip, rate)[: int(secs * rate)]
    dur_s = int16.size / rate
    gain = _compute_normalization_gain(int(np.abs(int16).max()))
    floats = np.clip((int16.astype(np.float32) / 32768.0) * gain, -1.0, 1.0)

    print(f"\n=== {label}: {Path(clip).name} @ {rate} Hz ({system}), {dur_s:.1f}s ===")
    rec = sd.rec(
        int((dur_s + 4.0) * CAP_SR), samplerate=CAP_SR, channels=2, device=device, dtype="float32"
    )
    time.sleep(1.5)

    api = Ultimate64API(url)
    streamer = AudioStreamer(
        api, rate, system, dither=False, digi_boost=True, dsp_params=DSPParams(enabled=True)
    )
    probe = RProbe(url)
    wav = str(d.stamped(f"nmirate_{label}", "wav"))
    try:
        api.reset()
        time.sleep(1.0)
        api.run_basic_clear_loop()
        streamer.start_for_external_source()
        probe.start()
        chunk = 1024  # larger feed chunk → fewer host-feed underruns so the higher
        #               rate isn't unfairly penalized vs the baseline in the A/B
        for i in range(0, floats.size, chunk):
            streamer._encode_and_enqueue(floats[i : i + chunk], block_on_full=True)
        deadline = time.time() + dur_s + 3.0
        while streamer.position_seconds() < dur_s - 0.1 and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(1.0)
    finally:
        probe.stop()
        streamer.stop()
        api.silence_sid()
        api.reset()
        api.close()

    sd.wait()
    save_wav(wav, rec.mean(axis=1).astype(np.float64), CAP_SR)
    r = probe.r_rate()
    pct = f"{100.0 * r / rate:.1f}%" if r else "n/a"
    flag = ""
    if r is not None and r < 0.95 * rate:
        flag = "  ** OVERRUN: R well below configured rate (handler can't keep up) **"
    print(f"    captured -> {wav}")
    print(
        f"    R (measured consumer) = {r:.0f} B/s vs {rate} configured ({pct}){flag}"
        if r
        else f"    R unmeasured ({probe.missed} missed polls){flag}"
    )
    return wav, r, rate


# --- two-domain analysis -------------------------------------------------
def _load(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64) / 32768.0
    return x


def _trim_silence(x: np.ndarray, sr: int) -> np.ndarray:
    """Drop leading/trailing near-silence (the ~1.5 s capture pad) by energy."""
    env = np.abs(x)
    thr = max(env.max() * 0.02, 1e-4)
    idx = np.where(env > thr)[0]
    if idx.size == 0:
        return x
    return x[max(0, idx[0] - sr // 20) : idx[-1] + sr // 20]


def _band_frac(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    """Fraction of spectral energy in [lo, hi] Hz (Welch-ish, 4096 windows)."""
    n = 4096
    if x.size < n:
        return float("nan")
    acc = np.zeros(n // 2 + 1)
    win = np.hanning(n)
    cnt = 0
    for i in range(0, x.size - n, n // 2):
        seg = x[i : i + n] * win
        acc += np.abs(np.fft.rfft(seg)) ** 2
        cnt += 1
    if not cnt:
        return float("nan")
    acc /= cnt
    f = np.fft.rfftfreq(n, 1.0 / sr)
    band = acc[(f >= lo) & (f < hi)].sum()
    return float(band / acc.sum()) if acc.sum() else float("nan")


def _metrics(x: np.ndarray, sr: int) -> dict:
    xt = _trim_silence(x, sr)
    rms = float(np.sqrt(np.mean(xt**2))) if xt.size else 0.0
    peak = float(np.abs(xt).max()) if xt.size else 0.0
    crest_db = 20 * np.log10(peak / rms) if rms > 0 else float("nan")
    return {
        "rms_db": 20 * np.log10(rms) if rms > 0 else float("nan"),
        "crest_db": crest_db,
        "hi_band_frac": _band_frac(xt, sr, *HI_BAND),
        "full_band_frac": _band_frac(xt, sr, 0, sr / 2),
    }


def analyze(wav_a: str, wav_b: str, rate_a: int, rate_b: int, tag: str) -> None:
    a, b = _load(wav_a), _load(wav_b)
    ma, mb = _metrics(a, CAP_SR), _metrics(b, CAP_SR)
    print(f"\n--- analysis [{tag}] : A={rate_a}Hz  B={rate_b}Hz ---")
    print(f"  {'metric':<16}{'A':>12}{'B':>12}{'Δ(B-A)':>12}")
    for k, unit in [
        ("rms_db", "dB"),
        ("crest_db", "dB"),
        ("hi_band_frac", f"frac {int(HI_BAND[0])}-{int(HI_BAND[1])}Hz"),
        ("full_band_frac", "frac"),
    ]:
        va, vb = ma[k], mb[k]
        print(f"  {k:<16}{va:>12.4f}{vb:>12.4f}{vb - va:>+12.4f}  {unit}")
    hi_gain = (mb["hi_band_frac"] / ma["hi_band_frac"]) if ma["hi_band_frac"] else float("nan")
    print(
        f"  → 4-5.5kHz captured energy B/A ratio = {hi_gain:.2f}x "
        f"(>1 means the higher rate reproduced new high-band content)"
    )
    _plot(a, b, rate_a, rate_b, tag)


def _envelope(x: np.ndarray, sr: int) -> np.ndarray:
    """Rectify + 1-pole lowpass (~30 Hz) amplitude envelope."""
    rc = 1.0 / (2 * np.pi * 30.0)
    alpha = (1.0 / sr) / (rc + 1.0 / sr)
    env = np.empty_like(x)
    acc = 0.0
    rect = np.abs(x)
    for i in range(x.size):  # small captures; readable over vectorized IIR
        acc += alpha * (rect[i] - acc)
        env[i] = acc
    return env


def _plot(a: np.ndarray, b: np.ndarray, rate_a: int, rate_b: int, tag: str) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  (matplotlib unavailable — numeric only)")
        return
    a, b = _trim_silence(a, CAP_SR), _trim_silence(b, CAP_SR)
    fig, ax = plt.subplots(2, 2, figsize=(13, 7))
    for col, (x, rate) in enumerate([(a, rate_a), (b, rate_b)]):
        t = np.arange(x.size) / CAP_SR
        ax[0, col].plot(t, x, lw=0.3, color="0.7")
        # decimate the slow IIR envelope for plotting cost
        env = _envelope(x[::1], CAP_SR)
        ax[0, col].plot(t, env, color="C3", lw=1.0)
        ax[0, col].plot(t, -env, color="C3", lw=1.0)
        ax[0, col].set_title(f"{tag}  {rate} Hz — waveform + envelope")
        ax[0, col].set_xlabel("s")
        ax[1, col].specgram(x, NFFT=1024, Fs=CAP_SR, noverlap=512, cmap="magma")
        ax[1, col].axhline(HI_BAND[0], color="cyan", lw=0.6, ls="--")
        ax[1, col].axhline(HI_BAND[1], color="cyan", lw=0.6, ls="--")
        ax[1, col].set_ylim(0, 8000)
        ax[1, col].set_title(
            f"{rate} Hz — spectrogram (cyan = {int(HI_BAND[0])}-{int(HI_BAND[1])}Hz)"
        )
        ax[1, col].set_xlabel("s")
        ax[1, col].set_ylabel("Hz")
    fig.tight_layout()
    png = str(d.stamped(f"nmirate_{tag}", "png"))
    fig.savefig(png, dpi=90)
    plt.close(fig)
    print(f"  plot -> {png}")


# --- System Mode reconfigure ---------------------------------------------
def ensure_system_mode(url: str, mode: str) -> None:
    cur = (d.rest_get_config(SYS_CATEGORY, url) or {}).get(SYS_SETTING)
    print(f"[sys] current System Mode = {cur!r}, want {mode!r}")
    if cur == mode:
        return
    if not d.rest_set_config(SYS_CATEGORY, SYS_SETTING, mode, url):
        raise SystemExit(f"[sys] FAILED to set System Mode = {mode}")
    print(f"[sys] set System Mode = {mode}; rebooting U64 to apply FPGA timing…")
    d.rest_reboot(url)
    time.sleep(8.0)
    for _ in range(40):  # up to ~80 s for the unit to return
        if d.rest_ping(url) == 200:
            break
        time.sleep(2.0)
    else:
        raise SystemExit("[sys] U64 did not come back after reboot")
    time.sleep(3.0)
    got = (d.rest_get_config(SYS_CATEGORY, url) or {}).get(SYS_SETTING)
    print(f"[sys] back up; System Mode now = {got!r}")
    if got != mode:
        raise SystemExit(f"[sys] reboot did not apply mode (still {got!r})")
    # The reboot dropped + re-negotiated HDMI, so the Cam Link capture device
    # hotplugged out from under PortAudio (it caches the device list at init).
    # Settle the HDMI link, then force PortAudio to re-enumerate, or the first
    # sd.rec() after the switch fails with PaErrorCode -9986 (internal error).
    print("[sys] settling HDMI + re-initializing PortAudio for the capture device…")
    time.sleep(10.0)
    sd._terminate()
    sd._initialize()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("clips", nargs="+", help="wideband audio file(s) under assets/audio/")
    ap.add_argument(
        "--system",
        choices=["NTSC", "PAL"],
        default="NTSC",
        help="latch math standard; MUST match U64 System Mode (see --switch-system)",
    )
    ap.add_argument(
        "--switch-system",
        action="store_true",
        help="set U64 System Mode to --system over REST + reboot before running",
    )
    ap.add_argument("--rate-a", type=int, default=8000, help="pass-A sample rate (baseline)")
    ap.add_argument("--rate-b", type=int, default=10500, help="pass-B sample rate (candidate)")
    ap.add_argument("--secs", type=float, default=15.0)
    ap.add_argument("--device", type=int, default=CAP_DEVICE, help="Cam Link audio sd index")
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--reverse", action="store_true", help="play B before A")
    args = ap.parse_args()

    for c in args.clips:
        if not Path(c).exists():
            ap.error(f"clip not found: {c}")

    if args.switch_system:
        ensure_system_mode(args.url, args.system)
    else:
        cur = (d.rest_get_config(SYS_CATEGORY, args.url) or {}).get(SYS_SETTING)
        if cur != args.system:
            print(
                f"[WARN] U64 System Mode is {cur!r} but --system={args.system!r} — "
                f"latch math will mismatch real timing. Use --switch-system."
            )

    rates = (
        [(args.rate_b, "B"), (args.rate_a, "A")]
        if args.reverse
        else [(args.rate_a, "A"), (args.rate_b, "B")]
    )
    for clip in args.clips:
        stem = Path(clip).stem[:16]
        results: dict[str, str] = {}
        for rate, side in rates:
            wav, _, _ = play_one(
                args.url,
                clip,
                rate,
                args.system,
                args.secs,
                args.device,
                f"{args.system}_{stem}_{side}",
            )
            results[side] = wav
            time.sleep(0.5)
        analyze(results["A"], results["B"], args.rate_a, args.rate_b, f"{args.system}_{stem}")

    print(
        "\nDone. EARS verdict: at rate-b, is speech more intelligible / are "
        "fricatives back — and is pitch/tempo clean (no overrun wobble)?"
    )
    print("Remember to restore System Mode → NTSC at end of session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
