"""Spatial dithering for pre-quantization color shaping.

Two families, selected by ``[color].dither``:

- **Ordered (Bayer 8×8)** — a fixed, position-deterministic threshold offset
  added to every pixel before nearest-palette quantization. Vectorized (one
  array op over the whole frame) and temporally stable (the same pixel
  position always gets the same offset), so it holds realtime frame rates
  without adding frame-to-frame shimmer. `bayer_offset` is the primitive;
  callers add its output to the pixel array before quantizing.
- **Error diffusion (Floyd-Steinberg / Atkinson)** — a sequential per-pixel
  scan that pushes each pixel's quantization error onto its yet-unvisited
  neighbors. Higher quality on static content (no competing candidate set
  reproduces gradients as well) but a Python-level loop — too slow for
  realtime video, and diffusing across frames independently makes each
  frame's error pattern independent of the last, which reads as shimmer on
  motion. `error_diffuse` is the primitive; callers run it once per
  candidate-set region (e.g. once per display cell) against that region's
  resolved palette subset.

Both primitives take an explicit BGR candidate set rather than the fixed
16-color C64 palette, so the same code dithers a global 16-color pass or a
per-cell subset (e.g. mhires' per-cell {bg0, c1, c2, c3}) identically.
"""

from __future__ import annotations

import numpy as np

DITHER_METHODS: tuple[str, ...] = ("none", "ordered", "floyd-steinberg", "atkinson")

# 8x8 Bayer ordered-dither threshold matrix (index-value form, 0..63).
_BAYER_8X8 = np.array(
    [
        [0, 32, 8, 40, 2, 34, 10, 42],
        [48, 16, 56, 24, 50, 18, 58, 26],
        [12, 44, 4, 36, 14, 46, 6, 38],
        [60, 28, 52, 20, 62, 30, 54, 22],
        [3, 35, 11, 43, 1, 33, 9, 41],
        [51, 19, 59, 27, 49, 17, 57, 25],
        [15, 47, 7, 39, 13, 45, 5, 37],
        [63, 31, 55, 23, 61, 29, 53, 21],
    ],
    dtype=np.float32,
)
# Normalized to a zero-mean -0.5..~0.48 range so `bayer_offset` can scale it
# by an arbitrary strength without a magic 64 divisor at every call site.
_BAYER_NORM = (_BAYER_8X8 / 64.0) - 0.5

# Floyd-Steinberg error-diffusion kernel: (dx, dy, weight/16), applied to the
# 4 unvisited neighbors of a raster-scan pixel.
_FS_KERNEL: tuple[tuple[int, int, float], ...] = (
    (1, 0, 7 / 16),
    (-1, 1, 3 / 16),
    (0, 1, 5 / 16),
    (1, 1, 1 / 16),
)
# Atkinson diffuses only 3/4 of the error (the rest is dropped), which keeps
# contrast punchier than Floyd-Steinberg at the cost of losing some detail in
# deep shadows/highlights — the classic Mac-era look.
_ATKINSON_KERNEL: tuple[tuple[int, int, float], ...] = tuple(
    (dx, dy, 1 / 8) for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))
)

_ERROR_DIFFUSION_KERNELS = {
    "floyd-steinberg": _FS_KERNEL,
    "atkinson": _ATKINSON_KERNEL,
}


def bayer_offset(h: int, w: int, strength: float) -> np.ndarray:
    """Return an (h, w) float32 additive offset from the tiled 8×8 Bayer matrix.

    Add this to a pixel array's every channel before nearest-palette
    quantization: pixels below the local threshold get pushed toward the next
    palette entry down, pixels above toward the next one up, and the fixed
    8×8 tiling means the same screen position always gets the same push — so
    a static source dithers identically frame to frame (no shimmer) while a
    moving one still gets full ordered-dither texture. `strength` scales the
    offset's total range (roughly ±32 * strength at strength's face value)."""
    tiles_y = -(-h // 8)
    tiles_x = -(-w // 8)
    tiled = np.tile(_BAYER_NORM, (tiles_y, tiles_x))[:h, :w]
    return tiled * (strength * 64.0)


def error_diffuse(
    img_bgr: np.ndarray,
    candidates_bgr: np.ndarray,
    method: str,
    strength: float = 1.0,
) -> np.ndarray:
    """Floyd-Steinberg / Atkinson dither `img_bgr` against a fixed candidate set.

    img_bgr: (h, w, 3) BGR, any numeric dtype. candidates_bgr: (k, 3) BGR.
    Returns an (h, w) uint8 array of indices into `candidates_bgr` (0..k-1).

    A plain per-pixel raster scan (Python-level loop — not realtime; see the
    module docstring): at each pixel, picks the nearest candidate by squared
    BGR distance, then pushes the quantization error onto not-yet-visited
    neighbors per `method`'s kernel, scaled by `strength` (1.0 = the
    textbook kernel weights; lower softens the diffusion toward a flatter,
    more `ordered`-like result).
    """
    kernel = _ERROR_DIFFUSION_KERNELS.get(method)
    if kernel is None:
        raise ValueError(
            f"error_diffuse: method must be one of {tuple(_ERROR_DIFFUSION_KERNELS)}, got {method!r}"
        )
    h, w = img_bgr.shape[:2]
    buf = img_bgr.astype(np.float32).copy()
    cand = np.asarray(candidates_bgr, dtype=np.float32)
    codes = np.empty((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            px = buf[y, x]
            d = ((cand - px) ** 2).sum(axis=1)
            idx = int(np.argmin(d))
            codes[y, x] = idx
            err = (px - cand[idx]) * strength
            for dx, dy, frac in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    buf[ny, nx] += err * frac
    return codes


def error_diffuse_cells(
    pixels: np.ndarray,
    candidates: np.ndarray,
    method: str,
    strength: float = 1.0,
) -> np.ndarray:
    """Batched per-cell error diffusion: N independent small regions, each
    diffused against its OWN candidate set, in lockstep across cells.

    pixels: (N, H, W, 3) BGR — N independent cells (no diffusion carries
    across a cell boundary, matching each display cell picking its own
    {bg0, c1, c2, ...} palette subset). candidates: (N, K, 3) BGR, one
    K-color candidate set per cell (K constant across cells; pad short
    per-cell sets by repeating an existing candidate — a repeat can only tie,
    never win a pixel it wouldn't otherwise). Returns (N, H, W) uint8 codes,
    each 0..K-1 indexing that cell's own candidate row.

    Loops over the H*W in-cell positions (small: 32 pixels for an mhires
    cell, 4 for MCM) with every step vectorized across all N cells at once,
    rather than looping N times with a small per-cell scan — same math, far
    fewer Python-level iterations.
    """
    kernel = _ERROR_DIFFUSION_KERNELS.get(method)
    if kernel is None:
        raise ValueError(
            f"error_diffuse_cells: method must be one of {tuple(_ERROR_DIFFUSION_KERNELS)}, got {method!r}"
        )
    n, h, w = pixels.shape[:3]
    buf = pixels.astype(np.float32).copy()
    cand = np.asarray(candidates, dtype=np.float32)
    codes = np.empty((n, h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            px = buf[:, y, x, :]  # (N, 3)
            d = ((cand - px[:, None, :]) ** 2).sum(axis=2)  # (N, K)
            idx = d.argmin(axis=1)  # (N,)
            codes[:, y, x] = idx
            chosen = np.take_along_axis(cand, idx[:, None, None], axis=1)[:, 0, :]  # (N, 3)
            err = (px - chosen) * strength
            for dx, dy, frac in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    buf[:, ny, nx, :] += err * frac
    return codes
