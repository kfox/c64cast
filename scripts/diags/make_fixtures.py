#!/usr/bin/env python3
"""Generate synthetic A/V fixtures for testing the commercial / audio paths
without depending on a real clip. Replaces the pile of one-off ffmpeg
``lavfi`` invocations that kept accumulating in settings.local.json.

    scripts/diags/make_fixtures.py tone        # 30s 440Hz tone wav
    scripts/diags/make_fixtures.py clip         # red video + 440Hz tone mp4
    scripts/diags/make_fixtures.py pattern      # SMPTE-ish testsrc + tone mp4
    scripts/diags/make_fixtures.py all
    scripts/diags/make_fixtures.py tone --freq 1000 -t 10

All outputs land under scripts/diags/out/. The mp4s are commercial-scene
inputs (point a `type = "commercial"` config's `file` at them).
"""

from __future__ import annotations

import argparse
import subprocess
import sys

import _diaglib as d

SR = 48000


def _run(cmd: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *cmd], check=True)


def tone(freq: float, seconds: float) -> str:
    out = str(d.out_dir() / f"tone_{int(freq)}hz_{int(seconds)}s.wav")
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate={SR}:duration={seconds}",
            "-ar",
            str(SR),
            out,
        ]
    )
    print(f"wrote {out}")
    return out


def clip(freq: float, seconds: float) -> str:
    out = str(d.out_dir() / f"clip_{int(freq)}hz_{int(seconds)}s.mp4")
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s=320x180:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate={SR}:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            out,
        ]
    )
    print(f"wrote {out}")
    return out


def pattern(freq: float, seconds: float) -> str:
    out = str(d.out_dir() / f"pattern_{int(seconds)}s.mp4")
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x180:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate={SR}:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            out,
        ]
    )
    print(f"wrote {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("kind", choices=["tone", "clip", "pattern", "all"])
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("-t", "--seconds", type=float, default=30.0)
    args = ap.parse_args()

    if args.kind in ("tone", "all"):
        tone(args.freq, args.seconds)
    if args.kind in ("clip", "all"):
        clip(args.freq, args.seconds)
    if args.kind in ("pattern", "all"):
        pattern(args.freq, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
