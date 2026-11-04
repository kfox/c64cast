"""Quality metrics for scoring a rendered C64 frame against its source frame,
and for scoring a shot-cut detector against hand-labeled cuts.

Every image argument is a 320x200 BGR uint8 frame, or the OKLab float frame
`oklab` makes from one. The rendered side is `Framebuffer.render()`; the
source side is the source frame cropped as c64cast crops it and area-resized
to the same 320x200, so both cover the same pixels at the same geometry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

CELL_PX = 8
CELL_ROWS = 25
CELL_COLS = 40

# The eye averages a dither pattern into the color it approximates, so a plain
# per-pixel error penalizes good dither (Kite 1998). Both frames are low-passed
# first. One mhires pixel is 2x1 px at 320x200; sigma 1.5 px spans it.
DEFAULT_BLUR_SIGMA_PX = 1.5

# SSIM's own window and constants, from Wang et al. 2004, for a signal in 0..1.
_SSIM_SIGMA = 1.5
_SSIM_C1 = 0.01**2
_SSIM_C2 = 0.03**2

_SRGB_TO_LINEAR = np.array(
    [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in np.arange(256) / 255.0],
    dtype=np.float32,
)
# Björn Ottosson's published OKLab matrices (linear sRGB -> LMS -> OKLab).
_LINEAR_RGB_TO_LMS = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ],
    dtype=np.float32,
)
_LMS_TO_OKLAB = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ],
    dtype=np.float32,
)


def linear_rgb(bgr: np.ndarray) -> np.ndarray:
    """Linear-light RGB, float32 in 0..1, from an sRGB BGR uint8 frame."""
    return _SRGB_TO_LINEAR[bgr[..., ::-1]]


def oklab_from_linear(rgb: np.ndarray) -> np.ndarray:
    lms = np.cbrt(rgb @ _LINEAR_RGB_TO_LMS.T)
    return lms @ _LMS_TO_OKLAB.T


def oklab(bgr: np.ndarray) -> np.ndarray:
    return oklab_from_linear(linear_rgb(bgr))


def blurred_oklab(bgr: np.ndarray, sigma_px: float = DEFAULT_BLUR_SIGMA_PX) -> np.ndarray:
    """The frame as the eye sees it from viewing distance: blurred in linear
    light, where the light from neighboring pixels actually mixes, then taken
    to OKLab."""
    rgb = linear_rgb(bgr)
    blurred = cv2.GaussianBlur(rgb, (0, 0), sigma_px, borderType=cv2.BORDER_REFLECT)
    return oklab_from_linear(blurred)


def oklab_mse(a_lab: np.ndarray, b_lab: np.ndarray) -> float:
    """Mean over pixels of the squared OKLab distance."""
    return float(np.mean(np.sum((a_lab - b_lab) ** 2, axis=-1)))


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Mean SSIM of two single-channel float images in 0..1."""

    def blur(x: np.ndarray) -> np.ndarray:
        return cv2.GaussianBlur(x, (0, 0), _SSIM_SIGMA, borderType=cv2.BORDER_REFLECT)

    a = a.astype(np.float32)
    b = b.astype(np.float32)
    mu_a, mu_b = blur(a), blur(b)
    var_a = blur(a * a) - mu_a * mu_a
    var_b = blur(b * b) - mu_b * mu_b
    cov = blur(a * b) - mu_a * mu_b
    num = (2 * mu_a * mu_b + _SSIM_C1) * (2 * cov + _SSIM_C2)
    den = (mu_a * mu_a + mu_b * mu_b + _SSIM_C1) * (var_a + var_b + _SSIM_C2)
    return float(np.mean(num / den))


def _cells(img: np.ndarray) -> np.ndarray:
    """(25, 40, 8, 8, ...) view of a 200x320 frame, one 8x8 block per C64 cell."""
    rest = img.shape[2:]
    return img.reshape(CELL_ROWS, CELL_PX, CELL_COLS, CELL_PX, *rest).swapaxes(1, 2)


def cells_changed(prev: np.ndarray, cur: np.ndarray) -> np.ndarray:
    """(25, 40) bool: cells in which any displayed pixel differs."""
    diff = prev != cur
    return _cells(diff).reshape(CELL_ROWS, CELL_COLS, -1).any(axis=-1)


def cell_mean_delta(prev_lab: np.ndarray, cur_lab: np.ndarray) -> np.ndarray:
    """(25, 40) mean OKLab distance per cell between two frames."""
    dist = np.sqrt(np.sum((prev_lab - cur_lab) ** 2, axis=-1))
    return _cells(dist).mean(axis=(2, 3))


def post_cut_windows(frames: int, cuts: Sequence[int], window: int = 30) -> list[range]:
    """The frames each cut affects: from the cut's frame (the new shot's
    first) for `window` frames, stopping at the next cut. `cuts` are frame
    indices; ones outside 0..frames are dropped.

    A shot's error is compared between two renders of the same frames, one
    with a change at the cut and one without: the difference is what the
    change bought. A shot's own later frames are no baseline, since smoothing
    can keep a whole shot off target rather than leave a ghost that fades."""
    bounds = sorted({c for c in cuts if 0 <= c < frames})
    ends = [*bounds[1:], frames]
    return [range(start, min(start + window, end)) for start, end in zip(bounds, ends, strict=True)]


@dataclass(frozen=True)
class Match:
    true_positives: list[tuple[int, int]]
    false_positives: list[int]
    false_negatives: list[int]

    @property
    def precision(self) -> float:
        found = len(self.true_positives) + len(self.false_positives)
        return len(self.true_positives) / found if found else 0.0

    @property
    def recall(self) -> float:
        real = len(self.true_positives) + len(self.false_negatives)
        return len(self.true_positives) / real if real else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def match_events(predicted: Sequence[int], truth: Sequence[int], tolerance: int = 1) -> Match:
    """Pair each true cut with at most one detection within `tolerance`
    frames, closest pairs first, so one detection can never claim two cuts."""
    pairs = sorted(
        (abs(p - t), p, t) for p in set(predicted) for t in set(truth) if abs(p - t) <= tolerance
    )
    used_p: set[int] = set()
    used_t: set[int] = set()
    tps: list[tuple[int, int]] = []
    for _, p, t in pairs:
        if p in used_p or t in used_t:
            continue
        used_p.add(p)
        used_t.add(t)
        tps.append((p, t))
    fps = sorted(set(predicted) - used_p)
    fns = sorted(set(truth) - used_t)
    return Match(sorted(tps, key=lambda pt: pt[1]), fps, fns)
