#!/usr/bin/env python3
"""Generate the blue-noise ordered-dither threshold matrix baked into
c64cast/dither.py as `_BLUE_NOISE_B64`.

Void-and-cluster (Ulichney 1993): starting from a random binary pattern,
repeatedly swap the "tightest cluster" (a 1 with the most 1-neighbors, found
via a Gaussian-blurred density map) for the "largest void" (a 0 with the
fewest 1-neighbors) until the pattern stops changing — this relaxes an
arbitrary seed into a prototype binary pattern (PBP) with no low-frequency
energy, i.e. no visible clumps or grid structure. Two ranking passes then
turn the PBP into a full 0..N-1 permutation: rank the PBP's own 1s downward
by repeatedly removing the tightest cluster, then rank every remaining 0
upward by repeatedly filling the largest void. Every rank is assigned
exactly once, so a threshold test `pixel > rank/N` at any cut point yields a
blue-noise binary pattern — which is exactly what an ordered-dither array
needs (c.f. `_BAYER_8X8`, which is the equivalent construction for a
regular, non-blue-noise threshold set).

The blur uses a toroidal (wraparound) Gaussian via FFT convolution so the
resulting matrix tiles seamlessly, matching `bayer_offset`'s tiling contract
— no numpy-only dependency added (no scipy).

Usage:
    uv run python scripts/diags/gen_blue_noise.py [--size 64] [--sigma 1.9] [--seed 0]

Prints the `_BLUE_NOISE_B64` literal to paste into c64cast/dither.py.
"""

from __future__ import annotations

import argparse
import base64

import numpy as np


def _toroidal_gaussian_kernel_fft(size: int, sigma: float) -> np.ndarray:
    ax = np.arange(size)
    ax = np.minimum(ax, size - ax)  # wraparound distance to nearest tile copy
    yy, xx = np.meshgrid(ax, ax, indexing="ij")
    kernel = np.exp(-(xx**2 + yy**2) / (2 * sigma**2))
    kernel /= kernel.sum()
    return np.fft.fft2(kernel)


def _blur(pattern: np.ndarray, kernel_fft: np.ndarray) -> np.ndarray:
    return np.real(np.fft.ifft2(np.fft.fft2(pattern) * kernel_fft))


def _tightest_cluster(pattern: np.ndarray, blurred: np.ndarray) -> tuple[int, int]:
    masked = np.where(pattern == 1, blurred, -np.inf)
    return np.unravel_index(np.argmax(masked), pattern.shape)  # type: ignore[return-value]


def _largest_void(pattern: np.ndarray, blurred: np.ndarray) -> tuple[int, int]:
    masked = np.where(pattern == 0, blurred, np.inf)
    return np.unravel_index(np.argmin(masked), pattern.shape)  # type: ignore[return-value]


def generate(size: int, sigma: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    kernel_fft = _toroidal_gaussian_kernel_fft(size, sigma)
    n = size * size
    n0 = max(1, n // 10)

    pattern = np.zeros((size, size), dtype=np.float64)
    pattern.flat[rng.choice(n, n0, replace=False)] = 1

    # Phase 0: relax the random seed into a prototype binary pattern by
    # repeatedly swapping the tightest cluster for the largest void until the
    # swap is a no-op (converged) or we've done enough iterations to be sure.
    for _ in range(4 * n0):
        blurred = _blur(pattern, kernel_fft)
        cy, cx = _tightest_cluster(pattern, blurred)
        pattern[cy, cx] = 0
        blurred = _blur(pattern, kernel_fft)
        vy, vx = _largest_void(pattern, blurred)
        pattern[vy, vx] = 1
        if (vy, vx) == (cy, cx):
            break

    ranks = np.zeros((size, size), dtype=np.int32)

    # Phase 1: rank the PBP's own ones downward, n0-1 .. 0, by repeatedly
    # removing the tightest cluster.
    working = pattern.copy()
    for rank in range(n0 - 1, -1, -1):
        blurred = _blur(working, kernel_fft)
        cy, cx = _tightest_cluster(working, blurred)
        working[cy, cx] = 0
        ranks[cy, cx] = rank

    # Phase 2: rank every remaining zero upward, n0 .. N-1, by repeatedly
    # filling the largest void (equivalent to Ulichney's phases 2+3 merged —
    # a standard simplification that still yields full-rank blue noise since
    # "largest void" stays well-defined as the pattern fills past 50%).
    working = pattern.copy()
    for rank in range(n0, n):
        blurred = _blur(working, kernel_fft)
        vy, vx = _largest_void(working, blurred)
        working[vy, vx] = 1
        ranks[vy, vx] = rank

    assert sorted(ranks.flatten().tolist()) == list(range(n)), "not a full permutation"
    return ranks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=64, help="tile side length (default 64)")
    parser.add_argument("--sigma", type=float, default=1.9, help="Gaussian blur sigma")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    ranks = generate(args.size, args.sigma, args.seed)
    packed = ranks.astype("<u2").tobytes()  # size*size fits in uint16 up to size=256
    blob = base64.b64encode(packed).decode("ascii")

    print(f"# size={args.size} sigma={args.sigma} seed={args.seed}")
    print(f"_BLUE_NOISE_SIZE = {args.size}")
    width = 76
    print("_BLUE_NOISE_B64 = (")
    for i in range(0, len(blob), width):
        print(f'    "{blob[i : i + width]}"')
    print(")")


if __name__ == "__main__":
    main()
