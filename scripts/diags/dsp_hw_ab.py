#!/usr/bin/env python3
"""Hardware A/B of a single [dsp] parameter on the U64 (host-DMA path).

Plays a clip twice through the production-default host-DMA path (decode →
peak-normalize → host DSP chain → 4-bit encode), varying ONE DSP parameter
between the two passes, and captures each off the Cam Link. Built to tune
quality knobs by ear (the only judge that matters on this hardware).

The immediate use is pre-emphasis for speech intelligibility — trailing
fricatives/stops ("ss", "d") get lost; pre-emphasis (an HF shelf applied before
the expander) lifts the recoverable 2-4 kHz consonant band ~3-4x at 0.5-0.8.

    scripts/diags/dsp_hw_ab.py CLIP --param pre_emphasis --a 0.0 --b 0.7
    scripts/diags/dsp_hw_ab.py assets/audio/OSR_us_male_0030_8k.wav --b 0.8

Host-DMA (not REU) on purpose: REU's tight ring margin laps on long clips
(separate transport issue); host-DMA plays clean and is the default path.

This needs your EARS — it makes sound on the real U64.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
import wave
from pathlib import Path

import _diaglib as d
import numpy as np
import sounddevice as sd

from c64cast.api import Ultimate64API
from c64cast.audio import AudioStreamer
from c64cast.dsp import DSPParams
from c64cast.video import _compute_normalization_gain, decode_audio_full

SR = 8000
CAP_SR = 48000
CAP_DEVICE = 1


def save_wav(path: str, mono: np.ndarray, sr: int) -> None:
    pcm = np.clip(mono * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def play(url: str, int16: np.ndarray, params: DSPParams, label: str,
         out_wav: str, device: int) -> None:
    """Play int16 mono through the host-DMA worker with the given DSP params."""
    dur_s = int16.size / SR
    cap_s = dur_s + 4.0
    print(f"\n=== {label}: {dur_s:.1f}s ===")
    rec = sd.rec(int(cap_s * CAP_SR), samplerate=CAP_SR, channels=2,
                 device=device, dtype="float32")
    time.sleep(1.5)

    api = Ultimate64API(url)
    streamer = AudioStreamer(api, SR, "NTSC", dither=False, digi_boost=True,
                             dsp_params=params)
    gain = _compute_normalization_gain(int(np.abs(int16).max()))
    floats = np.clip((int16.astype(np.float32) / 32768.0) * gain, -1.0, 1.0)
    try:
        api.reset()
        time.sleep(1.0)
        api.run_basic_clear_loop()
        streamer.start_for_external_source()
        # Rely on the encode queue's backpressure to self-pace (no manual sleep:
        # over-feeding laps the ~2 s queue and drops; under-feeding underruns).
        chunk = 512
        for i in range(0, floats.size, chunk):
            streamer._encode_and_enqueue(floats[i:i + chunk], block_on_full=True)
        deadline = time.time() + dur_s + 3.0
        while streamer.position_seconds() < dur_s - 0.1 and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(1.0)
    finally:
        streamer.stop()
        api.silence_sid()
        api.reset()
        api.close()

    sd.wait()
    save_wav(out_wav, rec.mean(axis=1).astype(np.float64), CAP_SR)
    print(f"    captured -> {out_wav}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", help="audio file under assets/audio/")
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--device", type=int, default=CAP_DEVICE)
    ap.add_argument("--secs", type=float, default=15.0)
    ap.add_argument("--param", default="pre_emphasis",
                    help="DSPParams field to vary between A and B")
    ap.add_argument("--a", type=float, default=0.0, help="value for pass A")
    ap.add_argument("--b", type=float, default=0.7, help="value for pass B")
    ap.add_argument("--reverse", action="store_true", help="play B first")
    args = ap.parse_args()

    if not Path(args.clip).exists():
        ap.error(f"clip not found: {args.clip}")
    if not any(f.name == args.param for f in dataclasses.fields(DSPParams)):
        ap.error(f"unknown DSPParams field: {args.param}")

    int16 = decode_audio_full(args.clip, SR)[: int(args.secs * SR)]
    print(f"clip {Path(args.clip).name}: {int16.size/SR:.1f}s; "
          f"A {args.param}={args.a}  vs  B {args.param}={args.b}")

    def mk(val: float) -> DSPParams:
        return dataclasses.replace(DSPParams(enabled=True), **{args.param: val})

    passes = [(f"B = {args.param} {args.b}", args.b),
              (f"A = {args.param} {args.a}", args.a)] if args.reverse else \
             [(f"A = {args.param} {args.a}", args.a),
              (f"B = {args.param} {args.b}", args.b)]
    for label, val in passes:
        tag = label.split("=")[0].strip()
        wav = str(d.stamped(f"dsp_ab_{tag}", "wav"))
        play(args.url, int16, mk(val), label, wav, args.device)
        time.sleep(0.5)

    print("\nDone. Which sounded better / more intelligible — A or B?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
