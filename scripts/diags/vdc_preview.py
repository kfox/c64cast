#!/usr/bin/env python3
"""Offline: show what an image will look like on the C128 VDC in 640x200
8x2-colour bitmap mode. **No hardware.**

Runs the same conversion the eventual ``VDCDisplayMode`` will use
(``c64cast.hw.vdc.pack_bitmap_frame``) and renders the packed frame straight
back (``vdc.simulate_frame``), so you can iterate on conversion quality and
source framing without a C128 or an RGBI capture card in the loop.

    scripts/diags/vdc_preview.py pic.jpg
    scripts/diags/vdc_preview.py pic.jpg --out preview.png --dump pic.vdc
    scripts/diags/vdc_preview.py pic.jpg --fit contain --aspect

Reports how many 8x2 blocks contained more than 2 colours (a colour "clash" —
those blocks lose detail), which is the number that predicts conversion quality.

``--dump`` writes the raw 24000-byte frame (16000 bitmap + 8000 attributes,
the VRAM layout at ``BITMAP_BASE`` / ``ATTR_BASE``) — the exact bytes an
on-C128 blit routine would DMA in.
"""

from __future__ import annotations

import argparse

import _diaglib as d
import numpy as np

from c64cast.hw import vdc


def load_and_frame(path: str, fit: str) -> np.ndarray:
    """Return a (200, 640, 3) RGB uint8 array from ``path``."""
    import cv2

    img = cv2.imread(path)
    if img is None:
        raise SystemExit(f"could not read image {path!r}")
    h, w = img.shape[:2]
    tw, th = vdc.BITMAP_W, vdc.BITMAP_H
    if fit == "stretch":
        out = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
    else:
        # 'contain' / 'cover' against the ~3.2:1 target aspect
        scale = (min if fit == "contain" else max)(tw / w, th / h)
        rw, rh = round(w * scale), round(h * scale)
        r = cv2.resize(img, (rw, rh), interpolation=cv2.INTER_AREA)
        out = np.zeros((th, tw, 3), dtype=np.uint8)
        y0, x0 = (th - rh) // 2, (tw - rw) // 2
        ys, xs = max(0, y0), max(0, x0)
        yr, xr = max(0, -y0), max(0, -x0)
        hh, ww = min(rh - yr, th - ys), min(rw - xr, tw - xs)
        out[ys : ys + hh, xs : xs + ww] = r[yr : yr + hh, xr : xr + ww]
    return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)


def clash_count(idx: np.ndarray) -> int:
    """How many 8x2 blocks hold more than 2 distinct palette indices."""
    blocks = idx.reshape(vdc.ATTR_ROWS, 2, vdc.ATTR_COLS, 8).transpose(0, 2, 1, 3)
    flat = blocks.reshape(vdc.ATTR_ROWS * vdc.ATTR_COLS, 16)
    return int(sum(len(np.unique(row)) > 2 for row in flat))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("image")
    ap.add_argument("--out", help="PNG path (default: out/vdc_preview_<stamp>.png)")
    ap.add_argument("--dump", help="also write the raw 24000-byte .vdc frame here")
    ap.add_argument(
        "--fit",
        choices=("stretch", "contain", "cover"),
        default="stretch",
        help="how to fit the source into 640x200 (default stretch)",
    )
    ap.add_argument(
        "--aspect",
        action="store_true",
        help="scale the preview PNG vertically x2 to approximate the on-screen aspect",
    )
    args = ap.parse_args()

    rgb = load_and_frame(args.image, args.fit)
    idx = vdc.quantize_to_vdc(rgb)
    bitmap, attr = vdc.pack_bitmap_frame(idx)
    shown = vdc.simulate_frame(bitmap, attr)  # (200, 640, 3) RGB

    clashes = clash_count(idx)
    total = vdc.ATTR_ROWS * vdc.ATTR_COLS
    print(f"source colours used:  {len(np.unique(idx))} / 16")
    print(f"colour clashes:       {clashes} / {total} blocks ({100 * clashes / total:.1f}%)")
    print("                      (blocks with >2 colours lose detail)")

    import cv2

    bgr = cv2.cvtColor(shown, cv2.COLOR_RGB2BGR)
    if args.aspect:
        bgr = cv2.resize(bgr, (vdc.BITMAP_W, vdc.BITMAP_H * 2), interpolation=cv2.INTER_NEAREST)
    out = args.out or str(d.stamped("vdc_preview", "png"))
    w, h = d.save_image(bgr, out, max_width=0)
    print(f"wrote {out}  ({w}x{h})")

    if args.dump:
        with open(args.dump, "wb") as f:
            f.write(bitmap + attr)
        print(f"wrote {args.dump}  ({vdc.FRAME_BYTES} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
