#!/usr/bin/env python3
"""Generate the User's Guide's placeholder figures and its cover logo.

The guide (docs/guide/) is illustrated with C64 screen captures. Until those
are grabbed off real hardware through a capture device, every figure is a
placeholder rendered at the final size, so the page layout is already
finished and swapping in a real capture is a pure file replacement.

Each placeholder is drawn in authentic C64 palette colors and states, in the
image itself, which capture it is standing in for -- so an unfinished figure
is obvious in the PDF rather than silently shipping.

    python scripts/make_guide_figures.py            # only missing figures
    python scripts/make_guide_figures.py --force    # redraw everything

Real captures drop straight onto the same paths (see docs/guide/img/README.md,
which this script regenerates from SHOT_LIST). A file is treated as a real
capture whenever it does not match, pixel for pixel, the placeholder we would
draw for it -- so --force redraws stale placeholders without ever clobbering
finished artwork. Pass --force-all if you really do mean to overwrite it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from c64cast.palette import C64_PALETTE_BGR  # noqa: E402

IMG_DIR = REPO_ROOT / "docs" / "guide" / "img"
SOURCE_LOGO = REPO_ROOT / "assets" / "logo.png"
COVER_LOGO = IMG_DIR / "logo-cover.png"

# A full PAL/NTSC frame including border, at 4x for print. The C64's 320x200
# active area sits inside a 384x272 frame; 4x gives ~1536px across, which is
# comfortably over 300dpi at the width the template places figures.
SCALE = 4
FRAME_W, FRAME_H = 384 * SCALE, 272 * SCALE
BORDER_X, BORDER_Y = 32 * SCALE, 36 * SCALE

# Written into the bottom-left of every generated placeholder. Its presence is
# how the script tells "still a placeholder" from "a real capture landed here".
PLACEHOLDER_MARK = "PLACEHOLDER"


def c64(index: int) -> tuple[int, int, int]:
    """A C64 palette entry as an OpenCV BGR tuple."""
    b, g, r = C64_PALETTE_BGR[index]
    return (int(b), int(g), int(r))


# figure path -> (border color, background color, headline, capture recipe)
#
# The recipe is the command a real capture should be taken from. It is printed
# into the placeholder AND into docs/guide/img/README.md, so the shot list
# can't drift from the figures.
SHOT_LIST: dict[str, tuple[int, int, str, str]] = {
    "fig-qs-1-hello.png": (
        0,
        6,
        "HELLO WORLD SCROLLER",
        "c64cast --config example:hello",
    ),
    "fig-qs-2-video.png": (
        0,
        0,
        "VIDEO IN MULTICOLOR HI-RES",
        "c64cast clip.mp4",
    ),
    "fig-ft-1-slideshow.png": (
        0,
        0,
        "SLIDESHOW OF STILL IMAGES",
        "c64cast assets/pictures/",
    ),
    "fig-1-1-doctor.png": (
        11,
        0,
        "DOCTOR OUTPUT (TERMINAL)",
        "c64cast --doctor --skip-probe",
    ),
    "fig-2-1-interstitial.png": (
        0,
        6,
        "UP NEXT INTERSTITIAL",
        "c64cast --config example:c64cast.example",
    ),
    "fig-2-2-wizard.png": (
        11,
        0,
        "THE --init WIZARD (TERMINAL)",
        "c64cast --init",
    ),
    "fig-3-1-waveform.png": (
        0,
        0,
        "SID OSCILLOSCOPE, THREE VOICES",
        "c64cast --config example:scene-waveform",
    ),
    "fig-3-2-generative.png": (
        0,
        0,
        "GENERATIVE PLASMA",
        "c64cast --config example:scene-generative-plasma",
    ),
    "fig-3-3-webcam.png": (
        0,
        0,
        "LIVE WEBCAM AS PETSCII",
        "c64cast --config example:scene-webcam-petscii",
    ),
    "fig-4-1-modes.png": (
        0,
        0,
        "THE SAME FRAME IN FOUR MODES",
        "one capture per [video].mode: petscii, mcm, hires, mhires",
    ),
    "fig-4-2-overlays.png": (
        0,
        6,
        "CLOCK AND SPECTRUM OVERLAYS",
        "c64cast --config example:overlay-clock",
    ),
}


def _put(
    img: np.ndarray,
    text: str,
    org: tuple[int, int],
    scale: float,
    color: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _text_width(text: str, scale: float, thickness: int = 2) -> int:
    (w, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    return w


def draw_placeholder(border: int, background: int, headline: str, recipe: str) -> np.ndarray:
    """Render one placeholder frame."""
    img = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    img[:, :] = c64(border)
    img[BORDER_Y : FRAME_H - BORDER_Y, BORDER_X : FRAME_W - BORDER_X] = c64(background)

    light = c64(1)  # white
    dim = c64(15)  # light gray
    accent = c64(3)  # cyan

    # A dashed inner rule, so the placeholder reads as deliberately unfinished.
    step = 16 * SCALE
    x0, y0 = BORDER_X + 6 * SCALE, BORDER_Y + 6 * SCALE
    x1, y1 = FRAME_W - BORDER_X - 6 * SCALE, FRAME_H - BORDER_Y - 6 * SCALE
    for x in range(x0, x1, step * 2):
        cv2.line(img, (x, y0), (min(x + step, x1), y0), dim, 2)
        cv2.line(img, (x, y1), (min(x + step, x1), y1), dim, 2)
    for y in range(y0, y1, step * 2):
        cv2.line(img, (x0, y), (x0, min(y + step, y1)), dim, 2)
        cv2.line(img, (x1, y), (x1, min(y + step, y1)), dim, 2)

    cx = FRAME_W // 2
    hs = 1.15 * SCALE / 4
    _put(img, headline, (cx - _text_width(headline, hs) // 2, FRAME_H // 2 - 8 * SCALE), hs, light)

    rs = 0.62 * SCALE / 4
    # The recipe can be long; wrap it on whitespace to fit the active area.
    avail = (FRAME_W - 2 * BORDER_X) - 12 * SCALE
    words, line, lines = recipe.split(" "), "", []
    for word in words:
        trial = f"{line} {word}".strip()
        if _text_width(trial, rs, 1) > avail and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    for i, ln in enumerate(lines):
        _put(
            img,
            ln,
            (cx - _text_width(ln, rs, 1) // 2, FRAME_H // 2 + (6 + i * 9) * SCALE),
            rs,
            accent,
            1,
        )

    ms = 0.55 * SCALE / 4
    mark_y = FRAME_H - BORDER_Y - 14 * SCALE
    _put(img, PLACEHOLDER_MARK, (BORDER_X + 5 * SCALE, mark_y), ms, dim, 1)
    return img


def is_generated_placeholder(path: Path, spec: tuple[int, int, str, str]) -> bool:
    """True when `path` still holds exactly the placeholder we would draw.

    Compared pixel-for-pixel against a freshly drawn frame rather than sniffed
    heuristically: a real capture saved over the same filename differs
    immediately, so `--force` can redraw stale placeholders without ever
    clobbering finished artwork.
    """
    img = cv2.imread(str(path))
    if img is None or img.shape != (FRAME_H, FRAME_W, 3):
        return False
    return bool(np.array_equal(img, draw_placeholder(*spec)))


def make_cover_logo() -> None:
    """Take the repo logo through to the cover as RGBA, and check its alpha.

    `assets/logo.png` carries its own anti-aliased transparency, so this is a
    guarantee rather than a transformation. It earns its place by failing
    loudly: an opaque logo composites onto the blue cover as a white
    rectangle, which is easy to ship and easy not to notice.
    """
    src = cv2.imread(str(SOURCE_LOGO), cv2.IMREAD_UNCHANGED)
    if src is None:
        raise SystemExit(f"cannot read {SOURCE_LOGO}")
    if src.ndim != 3 or src.shape[2] not in (3, 4):
        raise SystemExit(f"{SOURCE_LOGO}: expected a color image, got shape {src.shape}")
    if src.shape[2] == 3:
        raise SystemExit(f"{SOURCE_LOGO} has no alpha channel; the cover needs a transparent logo.")
    if int(src[:, :, 3].min()) == 255:
        raise SystemExit(
            f"{SOURCE_LOGO} is fully opaque; on the blue cover it would render as a "
            "white rectangle. Export it with transparency."
        )

    h, w = src.shape[:2]
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(COVER_LOGO), src)
    opaque = int((src[:, :, 3] > 0).sum())
    print(f"  {COVER_LOGO.relative_to(REPO_ROOT)}  ({opaque:,} visible px of {h * w:,})")


def write_shot_list() -> None:
    lines = [
        "# Guide figures",
        "",
        "Every image here is referenced by `docs/guide/*.md` and rendered into",
        "the PDF. Each one starts as a **placeholder** generated by",
        "[`scripts/make_guide_figures.py`](../../../scripts/make_guide_figures.py)",
        "and is finished by dropping a real capture onto the same filename.",
        "Anything that is not one of our generated placeholders is left alone by",
        "the generator, so a finished figure is never overwritten.",
        "",
        "The table below is the *intent* of each figure, not a record of how it",
        "was shot. For the settings and configs the current captures were",
        "actually made with, see [`../shots/README.md`](../shots/README.md).",
        "",
        "| Figure | Shows | Capture from |",
        "|---|---|---|",
    ]
    for name, (_, _, headline, recipe) in SHOT_LIST.items():
        lines.append(f"| `{name}` | {headline.title()} | `{recipe}` |")
    lines += [
        "",
        "`logo-cover.png` comes from `assets/logo.png` by the same script,",
        "which checks that the logo still carries the transparency the blue",
        "cover needs.",
        "",
    ]
    (IMG_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="redraw existing placeholders")
    ap.add_argument(
        "--force-all",
        action="store_true",
        help="redraw everything, including real captures (destructive)",
    )
    args = ap.parse_args()

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    print("cover logo:")
    make_cover_logo()

    print("figures:")
    for name, spec in SHOT_LIST.items():
        path = IMG_DIR / name
        if path.exists() and not args.force_all:
            if not is_generated_placeholder(path, spec):
                print(f"  {name}  kept (real capture)")
                continue
            if not args.force:
                print(f"  {name}  exists")
                continue
        cv2.imwrite(str(path), draw_placeholder(*spec))
        print(f"  {name}  drawn")

    write_shot_list()
    print(f"shot list: {(IMG_DIR / 'README.md').relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
