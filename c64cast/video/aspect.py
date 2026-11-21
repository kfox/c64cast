"""The shape a source picture is fit to before a display mode resizes it onto
the C64's 320x200 screen, and the crop/fit primitives that do the fitting.

See docs/architecture/video-color.md#aspectpy--the-screens-shape-and-fitting-a-source-to-it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from c64cast.hw.backend import HardwareProfile

SCREEN_WIDTH, SCREEN_HEIGHT = 320, 200

# Width over height of one VIC-II pixel as a 4:3 set shows the composite
# signal: VICE's vicii_get_pixel_aspect (src/vicii/vicii.c).
COMPOSITE_PIXEL_ASPECT = {"NTSC": 0.75, "PAL": 0.93650794}
# The Ultimate 64-II's HDMI scaler (1541ultimate software/u64/hdmi_scan.cc,
# SetVideoMode1080p): at 720p and 1080p it draws 2.5/3.75 output pixels across
# per VIC pixel against 3/4.5 down per NTSC raster line and 2.67/4 per PAL one.
HDMI_PIXEL_ASPECT = {"NTSC": 5 / 6, "PAL": 15 / 16}
SQUARE_PIXEL_ASPECT = {"NTSC": 1.0, "PAL": 1.0}

# Mirrored by config.PIXEL_ASPECT_CHOICES, which has to stay import-light.
PIXEL_ASPECT_CHOICES = ("auto", "composite", "hdmi", "square")
_PIXEL_ASPECTS = {
    "composite": COMPOSITE_PIXEL_ASPECT,
    "hdmi": HDMI_PIXEL_ASPECT,
    "square": SQUARE_PIXEL_ASPECT,
}


def pixel_aspect(choice: str, profile: HardwareProfile) -> float:
    """One C64 pixel's width over its height for `[hardware].pixel_aspect`
    ``choice`` on the machine ``profile`` describes. ``"auto"`` is the HDMI
    scaler's on an Ultimate 64 and the composite signal's anywhere else."""
    if choice == "auto":
        # Only an Ultimate 64 registers the System Mode category; an Ultimate
        # II+ or a TeensyROM+ sits in a real C64 whose VIC-II drives the set.
        choice = "hdmi" if profile.supports_system_mode else "composite"
    return _PIXEL_ASPECTS[choice][profile.system]


def screen_aspect(choice: str, profile: HardwareProfile) -> float:
    """Width over height of the 320x200 screen as it is seen, which is the
    shape a source is fit to: 1.2 on an NTSC composite picture, 1.6 only
    where the pixels are square."""
    return SCREEN_WIDTH * pixel_aspect(choice, profile) / SCREEN_HEIGHT


def crop_box(width: int, height: int, aspect: float) -> tuple[int, int, int, int]:
    """``(x0, y0, w, h)`` of the centered ``aspect`` window a center-crop of a
    ``width`` x ``height`` picture keeps."""
    ratio = width / height if height else aspect
    if ratio > aspect:
        keep = int(height * aspect)
        return (width - keep) // 2, 0, keep, height
    if ratio < aspect:
        keep = int(width / aspect)
        return 0, (height - keep) // 2, width, keep
    return 0, 0, width, height


def crop_to_aspect(img: np.ndarray, aspect: float) -> np.ndarray:
    """Center-crop ``img`` to ``aspect`` (fill/cover): the long axis loses its
    margins so the picture fills the screen edge to edge."""
    h, w = img.shape[:2]
    x0, y0, cw, ch = crop_box(w, h, aspect)
    if (cw, ch) == (w, h):
        return img
    return img[y0 : y0 + ch, x0 : x0 + cw]


def fit_to_aspect(
    img: np.ndarray,
    aspect: float,
    pad_color: tuple[int, int, int] = (0, 0, 0),
) -> np.ndarray:
    """Letterbox or pillarbox ``img`` to ``aspect`` (contain): the short axis
    is padded with ``pad_color`` so the whole picture shows. One solid color
    quantizes to one stable palette cell (black is C64 index 0)."""
    h, w = img.shape[:2]
    ratio = w / h if h else aspect
    if ratio > aspect:
        pad = max(0, round(w / aspect) - h)
        top = pad // 2
        return cv2.copyMakeBorder(img, top, pad - top, 0, 0, cv2.BORDER_CONSTANT, value=pad_color)
    if ratio < aspect:
        pad = max(0, round(h * aspect) - w)
        left = pad // 2
        return cv2.copyMakeBorder(img, 0, 0, left, pad - left, cv2.BORDER_CONSTANT, value=pad_color)
    return img


def apply_aspect(img: np.ndarray, aspect_mode: str, aspect: float) -> np.ndarray:
    """``img`` fit to ``aspect`` the way the slideshow's ``aspect_mode`` asks:
    ``"crop"`` fills, ``"fit"`` pads, ``"stretch"`` leaves it for the display
    mode's resize to distort."""
    if aspect_mode == "fit":
        return fit_to_aspect(img, aspect)
    if aspect_mode == "stretch":
        return img
    return crop_to_aspect(img, aspect)
