#!/usr/bin/env python3
"""Compare two Mahoney signed-calibration curves (two physical SID chips) to
quantify chip-to-chip variance and decide whether one shipped table generalizes.

Reads two ``mahoney_*_signed.csv`` files (columns: code, ..., signed_level) as
produced by ``mahoney_dac_calib.py --signed`` and reports:
  * absolute + shape (per-chip full-scale-normalized) per-code level deviation
    (max and RMS across all 256 codes),
  * how many of the 256 amplitude-index → code choices differ between the two
    chips' sidtables (the metric that actually matters for playback), and
  * the ladder uniformity of an averaged table.

    scripts/diags/mahoney_compare_chips.py A_signed.csv B_signed.csv
"""

from __future__ import annotations

import csv
import sys

import numpy as np


def load(path: str) -> tuple[np.ndarray, np.ndarray]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    code = np.array([int(r["code"]) for r in rows])
    lvl = np.array([float(r["signed_level"]) for r in rows])
    order = np.argsort(code)
    return code[order], lvl[order]


def sidtable(code: np.ndarray, lvl: np.ndarray) -> np.ndarray:
    """256 uniform target levels across the signed span → nearest-level code."""
    targets = np.linspace(lvl.min(), lvl.max(), 256)
    return np.array([int(code[np.argmin(np.abs(lvl - t))]) for t in targets])


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    ca, la = load(sys.argv[1])
    cb, lb = load(sys.argv[2])
    assert np.array_equal(ca, cb), "code sets differ"

    fa, fb = np.abs(la).max(), np.abs(lb).max()
    print(f"A: span {la.min():.4f}..{la.max():.4f}  full-scale |L| {fa:.4f}")
    print(f"B: span {lb.min():.4f}..{lb.max():.4f}  full-scale |L| {fb:.4f}")
    print(f"absolute full-scale ratio B/A = {fb / fa:.3f}")

    # Absolute deviation (same capture chain → directly comparable units).
    dabs = la - lb
    print(
        f"\nABSOLUTE per-code level deviation: max {np.abs(dabs).max():.4f}  "
        f"rms {np.sqrt(np.mean(dabs**2)):.4f}  ({np.sqrt(np.mean(dabs**2)) / fa * 100:.1f}% of A full-scale)"
    )

    # Shape deviation: normalize each chip to its own full-scale, then compare.
    na, nb = la / fa, lb / fb
    dsh = na - nb
    print(
        f"SHAPE (per-chip normalized) deviation: max {np.abs(dsh).max():.4f}  "
        f"rms {np.sqrt(np.mean(dsh**2)):.4f}"
    )
    print(f"correlation of curves: {np.corrcoef(la, lb)[0, 1]:.5f}")

    # The metric that matters: do the two sidtables pick different codes?
    ta, tb = sidtable(ca, la), sidtable(cb, lb)
    diff = np.sum(ta != tb)
    # How much does using A's table on chip B cost (level error)?
    lb_by_code = {int(c): float(v) for c, v in zip(cb, lb, strict=True)}
    targets_b = np.linspace(lb.min(), lb.max(), 256)
    err_using_a = np.array([lb_by_code[int(ta[i])] - targets_b[i] for i in range(256)])
    print(f"\nsidtable code choices differing A vs B: {diff}/256")
    print(
        f"level error from using A's table on chip B: max {np.abs(err_using_a).max():.4f}  "
        f"rms {np.sqrt(np.mean(err_using_a**2)):.4f}  ({np.sqrt(np.mean(err_using_a**2)) / fb * 100:.1f}% of B full-scale)"
    )

    # Averaged table (candidate shippable): mean of per-chip normalized levels.
    avg = (na + nb) / 2.0
    tavg = sidtable(ca, avg)
    gaps = np.diff(np.sort(avg[np.searchsorted(ca, tavg)]))
    print(
        f"\naveraged (normalized) table worst ladder gap: {gaps.max():.4f} of [-1,1] span "
        f"({gaps.max() / (avg.max() - avg.min()) * 100:.1f}%)"
    )
    print(
        "\nVERDICT GUIDE: shape rms < ~0.02 and <~20 differing codes → one table "
        "generalizes (ship averaged). Larger → per-unit calibration matters; "
        "get a 3rd chip / ship Mahoney's published averaged table."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
