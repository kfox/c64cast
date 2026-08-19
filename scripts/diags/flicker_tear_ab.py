#!/usr/bin/env python3
"""Does the bank swap ever land inside the visible picture? Measure how often,
and where.

The tear under test: the swap IRQ at line 248 writes $DD00 to make the newly
staged frame visible. A host-DMA write in flight halts the 6510 at ~1 cycle per
byte, deferring that write to the end of the halt; an 8000-byte bitmap push
caught mid-flight has ~4000 bytes left on average = ~62 raster lines, landing
the swap well inside the visible frame. The top band then still shows the
PREVIOUS frame while the rest shows the new one.

This is the acceptance test for the $D012 window gate in modes_irq's two
host-DMA swap handlers, which declines to commit outside vblank rather than
committing late. A pre-gate build measured 5.3% of flicker frames torn and 1.2%
of plain ones. With the gate, over 1796 scored frames per phase: plain 0 of
1796, flicker 0.28% with the seams scattered rather than at a fixed line, at
unchanged throughput (~235 KiB/s, clock/wall 1.0000). Throughput is the other
half of the result: a fix that trades frame rate for cleanliness is not the fix
this is checking for, so read the fps in the phase logs too.

The residual is flicker-only, which is what identifies it — the handler checks
$D012 and writes $DD00 a few cycles later, and a halt beginning in that gap
passes the check and commits late anyway. Expect roughly that, not zero, until
the staging path stops halting the bus.

Method. A synthetic video cycles 8 states. Colors alternate every frame so
consecutive displayed frames are always tellable apart, while the stripe phase
advances over a period of 8 so the bitmap is dirty on every frame in every bank.
The phase period must not be 2: that aliases exactly with the two-bank swap,
each bank keeps receiving the same state, the dirty diff skips almost
everything, and the link idles at 13 KiB/s instead of the 270 this is meant to
sustain. Region colors are exact C64 palette entries, so each quantizes to a
solid (c,c) pair and no cell blends: the $D018 alternation is armed and running
but invisible, which separates the bank-swap timing from the color blending it
normally rides with.

The staging path is pinned, not left on auto — push() tests use_reu_staged
before double_buffer, so on auto the control runs the REU pipeline and
flicker_tolerance stops being the only variable between the phases. The REU path
has its own, unrelated swap-timing defect; this script does not measure it.

Two phases:

  * flicker   — flicker_tolerance on. Commits only on phase 0, so it gets half as
                many commit opportunities and was the louder of the two.
  * plain     — flicker OFF, plain hires double-buffer. Control: that path
                swaps banks from the same line-248 IRQ, so if it tears too the
                fault is the host-DMA swap in general, not anything flicker
                added. It did, which is why the fix went into both handlers.

A frame is torn when its picture holds a long run of state-A rows and a long run
of state-B rows at once, classified per row against the known palette entries.
Both of a state's two colors map to that state, so the mid-picture color change
is not a false seam. The reported seam position — where the picture flips, as a
fraction of height — is the real payload: the halt hypothesis predicts where the
swap lands, not merely that it is late.

    scripts/diags/flicker_tear_ab.py
    scripts/diags/flicker_tear_ab.py --seconds 10

Outputs land under scripts/diags/out/flickertear/. Resets the U64 on exit —
note that rest_reset needs an http:// URL; on a u64:// URL it does nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

import _diaglib as d
import cv2
import numpy as np

# Exact palette entries (BGR) of the table the machine actually emits, so every
# region quantizes to a solid (c,c) pair at distance 0 and no cell blends. Taken
# from the code rather than transcribed, and the config pins host_palette to
# match — a mismatch here would silently reintroduce the blending this phase
# exists to hold still.
HOST_PALETTE = "u64"


def _palette() -> dict[str, tuple[int, int, int]]:
    from c64cast.video.palette import HOST_PALETTES

    table = HOST_PALETTES[HOST_PALETTE]
    idx = {"black": 0, "red": 2, "green": 5, "blue": 6, "yellow": 7}
    return {k: tuple(int(x) for x in table[i]) for k, i in idx.items()}


PAL = _palette()

# Frames in the test cycle. Must not be 2 — see build_test_video.
CYCLE = 8


def build_test_video(path: Path, *, fps: int = 30, seconds: int = 25) -> None:
    """Eight states in a cycle, at 320x200 (one C64 screen).

    Colors alternate every frame (A: top red / bottom blue, B: top green /
    bottom yellow) so consecutive displayed frames are always tellable apart.
    The stripe phase advances by one pixel every frame over a period of 8.

    That second period is load-bearing. A two-frame video aliases exactly with
    the two-bank swap: bank 0 lands every even frame and bank 1 every odd one,
    so each bank keeps receiving the same state, the per-bank dirty diff finds
    nothing changed, and the link goes idle — 654 of 996 region writes skipped,
    13 KiB/s instead of 270. That is the opposite of the sustained worst case
    this is supposed to hold. A phase period of 8 gives each bank four distinct
    bitmaps, so the 8000-byte bitmap push — the long halt under test — is dirty
    on every single frame.

    Encoded FFV1 rather than mp4v: the colors have to survive the codec bit-exact
    or they stop being palette entries and start quantizing to blends, which is
    the one thing this phase needs held still. mp4v's 4:2:0 chroma turned the
    five colors into 122 and blended half the cells.
    """
    w, h = 320, 200

    def field(top: str, bottom: str, phase: int) -> np.ndarray:
        # Colored stripes ON black, not black stripes on color: hires has a
        # single global background, so black has to be the most common color or
        # a region color takes bg0 and the other half is left fitting two
        # foregrounds against it — which it does by blending them, reintroducing
        # exactly the alternation this design removes.
        img = np.zeros((h, w, 3), np.uint8)
        # Split on a character-cell boundary. A cell straddling the two halves
        # holds both region colors, needs two foregrounds against the one
        # background, and blends them to get there.
        split = (h // 2 // 8) * 8
        for half, color in ((slice(0, split), top), (slice(split, h), bottom)):
            for k in range(3):  # 3 of every 8 columns = 37.5% color, 62.5% black
                img[half, (phase + k) % 8 :: 8] = PAL[color]
        return img

    tmp = path.parent / "_frames"
    tmp.mkdir(exist_ok=True)
    for i in range(CYCLE):
        top, bottom = ("red", "blue") if i % 2 == 0 else ("green", "yellow")
        cv2.imwrite(str(tmp / f"{i:05d}.png"), field(top, bottom, i))
    pair = tmp / "pair.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(tmp / "%05d.png"),
            "-c:v",
            "ffv1",
            "-pix_fmt",
            "bgr0",
            str(pair),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-stream_loop",
            str((fps * seconds) // CYCLE - 1),
            "-i",
            str(pair),
            "-c",
            "copy",
            str(path),
        ],
        check=True,
    )


def write_config(cfg_path: Path, video_path: Path, *, flicker: bool) -> None:
    cfg_path.write_text(
        f"""
[audio]
enabled = false

[hardware]
host_palette = "{HOST_PALETTE}"

[color]
dither = "none"
flicker_tolerance = "{"strobe" if flicker else "off"}"
flicker_max_luma_delta = 0.075

[video]
double_buffer = true
# Pinned, not left on auto. On auto this resolves to REU staging, and push()
# tests use_reu_staged before double_buffer — so the "plain" control silently
# ran the REU bank-swap pipeline instead of the host-DMA swap it is meant to
# be the control for, leaving flicker_tolerance not the only variable between them.
use_reu_staged = false

[playlist]
interleave_videos = false
loop = true

[[scenes]]
type = "video"
display = "hires"
file = "{video_path}"
"""
    )


def burst_capture(label: str, seconds: float, device) -> list[np.ndarray]:
    cap = d.open_capture(device)
    for _ in range(10):  # warmup / flush stale buffer
        cap.read()
    # Wait for signal before starting the clock. The capture card can take
    # seconds to relock after the mode change a phase launch causes, and a run
    # that starts early spends its whole window on a black screen — which the
    # old scorer folded into the tally instead of reporting.
    t_lock = time.monotonic() + 15.0
    while time.monotonic() < t_lock:
        ok, frame = cap.read()
        if ok and frame is not None and float(frame.mean()) * 3.0 >= BLANK_SUM:
            break
    else:
        print(f"[{label}] WARNING: no signal after 15s — capturing anyway")
    frames: list[np.ndarray] = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        ok, frame = cap.read()
        if ok and frame is not None:
            frames.append(frame)
    cap.release()
    print(f"[{label}] captured {len(frames)} frames")
    return frames


def run_phase(label, cfg, url, seconds, device, outdir) -> tuple[list[np.ndarray], str]:
    log = outdir / f"{label}.log"
    env = dict(os.environ)
    print(f"[{label}] launching c64cast …")
    with open(log, "w") as lf:
        proc = subprocess.Popen(
            [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", url, "-v"],
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            time.sleep(8.0)  # boot + first rendered frames
            frames = burst_capture(label, seconds, device)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    return frames, log.read_text()


# State A paints red over blue, state B green over yellow. Both of a state's
# colors map to that state, so a row is classified without caring which half of
# the picture it came from — and the red/blue boundary, which is a color change
# but NOT a state change, stops being a false seam.
_STATE_OF = {"red": "A", "blue": "A", "green": "B", "yellow": "B"}
BLANK_SUM = 18.0  # mean B+G+R below this is no signal, not a dark color
MIN_RUN = 10  # rows; shorter runs are boundary noise, not a tear


def _row_states(frame: np.ndarray) -> list[str]:
    """Classify every row of the active picture as state A, B, or blank.

    Deterministic, against the known palette entries. The previous version
    learned two clusters per region with a PCA median split, but the sign of an
    SVD component is arbitrary and the two regions were fitted independently —
    so their cluster labels could come out paired in either order, and when they
    came out crossed the comparison reported every clean frame as torn and every
    torn frame as clean. Nothing about that failure is visible in the output.
    """
    h, w, _ = frame.shape
    band = frame[:, int(w * 0.30) : int(w * 0.70)].astype(float)
    rows = band.mean(axis=1)  # (h, 3) mean BGR per row

    names = list(_STATE_OF)
    ref = np.array([PAL[n] for n in names], float)
    ref /= np.linalg.norm(ref, axis=1, keepdims=True)

    mag = rows.sum(axis=1)
    norm = np.linalg.norm(rows, axis=1, keepdims=True)
    unit = rows / np.maximum(norm, 1e-6)
    # Cosine against each palette entry: duty-cycle independent, so a row of
    # 37.5%-coverage stripes matches the same color as a solid one.
    best = (unit @ ref.T).argmax(axis=1)
    return ["blank" if mag[y] < BLANK_SUM else _STATE_OF[names[best[y]]] for y in range(h)]


def _runs(states: list[str]) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for i, st in enumerate(states):
        if out and out[-1][0] == st:
            out[-1] = (st, out[-1][1], i)
        else:
            out.append((st, i, i))
    return out


def score(frames) -> tuple[float, list[int], int, list[float]]:
    """Percent torn, their indices, the blank count, and the seam positions.

    A frame is torn when it holds a run of state-A rows and a run of state-B
    rows, both long enough not to be boundary noise. The seam position is where
    the picture flips, as a fraction of picture height — that is the payload
    this experiment actually wants, since the halt hypothesis predicts *where*
    the swap lands, not merely that it is late.
    """
    torn: list[int] = []
    seams: list[float] = []
    blank = 0
    for i, f in enumerate(frames):
        runs = [r for r in _runs(_row_states(f)) if r[2] - r[1] + 1 >= MIN_RUN]
        live = [r for r in runs if r[0] != "blank"]
        if not live:
            blank += 1
            continue
        if len({r[0] for r in live}) < 2:
            continue
        torn.append(i)
        y0, y1 = live[0][1], live[-1][2]
        for a, b in zip(live, live[1:], strict=False):
            if a[0] != b[0]:
                seams.append((b[1] - y0) / max(1, y1 - y0))
                break
    scored = len(frames) - blank
    return 100.0 * len(torn) / max(1, scored), torn, blank, seams


def log_facts(text: str) -> str:
    armed = "flicker blend armed" in text
    ver = "0.4.0" if '"c64cast_version": "0.4.0"' in text else "?"
    fps = [ln for ln in text.splitlines() if "fps" in ln.lower()][-1:]
    return f"armed={armed} version={ver} " + (f"| {fps[0].strip()[-90:]}" if fps else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--seconds", type=float, default=10.0, help="capture window per phase")
    ap.add_argument("--device", default=d.CAMLINK_DEVICE)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    out = d.out_dir() / "flickertear"
    out.mkdir(parents=True, exist_ok=True)
    video = out / "ab_every_frame.mkv"
    print(f"[build] test video -> {video}")
    build_test_video(video)

    cfg_flicker = out / "flicker.toml"
    cfg_plain = out / "plain.toml"
    write_config(cfg_flicker, video, flicker=True)
    write_config(cfg_plain, video, flicker=False)

    phases = [
        ("flicker", cfg_flicker),
        ("plain", cfg_plain),
    ]
    results: dict[str, list[np.ndarray]] = {}
    logs: dict[str, str] = {}
    try:
        for label, cfg in phases:
            frames, text = run_phase(label, cfg, args.url, args.seconds, args.device, out)
            results[label] = frames
            logs[label] = text
            print(f"[{label}] {log_facts(text)}")
            time.sleep(3.0)  # let the link settle between runs
    finally:
        if not args.no_reset:
            print(f"[reset] {args.url}: {d.rest_reset(args.url)}")

    if not any(results.values()):
        print("no frames captured — is the capture device attached?")
        return 1

    print("\n=== torn frames (picture holds both states at once) ===")
    for label, _cfg in phases:
        frames = results.get(label, [])
        pct, idx, blank, seams = score(frames)
        note = f"  [{blank} blank frames excluded]" if blank else ""
        print(f"  {label:9s} {pct:5.1f}%  ({len(idx)} of {len(frames) - blank} scored){note}")
        if seams:
            q = np.percentile(seams, [10, 50, 90])
            print(
                f"             seam at {q[0]:.0%} / {q[1]:.0%} / {q[2]:.0%} of "
                f"picture height (p10/median/p90)"
            )
        for n, i in enumerate(idx[:3]):
            cv2.imwrite(str(out / f"{label}_torn_{n:02d}.png"), frames[i])
        clean = [i for i in range(len(frames)) if i not in set(idx)]
        for n, i in enumerate(clean[len(clean) // 2 :][:2]):
            cv2.imwrite(str(out / f"{label}_clean_{n:02d}.png"), frames[i])
    print(f"\nframes + configs + logs: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
