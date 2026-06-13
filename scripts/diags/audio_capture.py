#!/usr/bin/env python3
"""Capture audio from the Cam Link (U64 HDMI audio) via ffmpeg/avfoundation
and report a level summary (ffmpeg ``volumedetect``). The objective-loudness
tool used when "I heard audio / I heard silence" needs to become a number.

    scripts/diags/audio_capture.py                 # 15s capture + analysis
    scripts/diags/audio_capture.py -t 30           # longer window
    scripts/diags/audio_capture.py --device :5     # avfoundation index drift
    scripts/diags/audio_capture.py --analyze x.wav # just analyze an existing file

Capture-window gotcha (bitten repeatedly): c64cast takes ~5s to boot + reach
the first PLAY tick, so START THE CAPTURE FIRST, then launch c64cast in
another shell, and make -t long enough to cover boot + scene + margin. The
run_and_capture.py harness handles that ordering for you; use this tool for
free-standing measurements.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

import _diaglib as d


def record(device: str, seconds: float, out_path: str) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-i", device,
        "-t", str(seconds), "-ac", "1", "-ar", "48000", out_path,
    ]
    print(f"recording {seconds:g}s from avfoundation {device} -> {out_path}")
    subprocess.run(cmd, check=True)


def analyze(path: str) -> None:
    """Run volumedetect and surface the mean/max dB lines."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
         "-vn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # volumedetect writes to stderr.
    wanted = re.compile(r"(mean_volume|max_volume|n_samples|histogram_0db)")
    lines = [ln.strip() for ln in r.stderr.splitlines() if wanted.search(ln)]
    if lines:
        print(f"--- volumedetect {path} ---")
        for ln in lines:
            print(ln)
    else:
        print(f"(no volumedetect output for {path}; ffmpeg stderr:)")
        print(r.stderr.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default=d.CAMLINK_AVF_AUDIO,
                    help=f"avfoundation audio device (default {d.CAMLINK_AVF_AUDIO}; "
                         "confirm with: ffmpeg -f avfoundation -list_devices true -i \"\")")
    ap.add_argument("-t", "--seconds", type=float, default=15.0)
    ap.add_argument("-o", "--out", default=None, help="output wav path")
    ap.add_argument("--analyze", metavar="WAV", default=None,
                    help="skip capture; just analyze this file")
    args = ap.parse_args()

    if args.analyze:
        analyze(args.analyze)
        return 0

    out_path = args.out or str(d.stamped("camlink_audio", "wav"))
    record(args.device, args.seconds, out_path)
    analyze(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
