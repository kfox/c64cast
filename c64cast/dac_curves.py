"""Baked $D418 DAC transfer tables (companding LUTs) for the Mahoney 8-bit
digi technique, plus the resolver that maps a ``[audio].dac_curve`` name to a
table (or ``None`` for the legacy linear 4-bit path).

Background — the Mahoney technique
----------------------------------
Pex 'Mahoney' Tufvesson's 2014 technique parks all three SID voices as steady
DC sources (pulse + TEST + GATE, ADSR sustained) with voices 1+2 routed through
the analog filter, then writes the **full 8-bit ``$D418`` byte** per sample:
the volume nibble (bits 0-3) *plus* the filter-mode bits HP/BP/LP (4-6) and the
"voice-3 OFF" bit (7). Those upper bits additively/subtractively re-route the
parked DC voices, so the master mixer emits ~256 distinct, strongly non-linear
output levels instead of the 16 the volume nibble alone gives — roughly 6-7
*effective* bits (Wothke's measurement; not literally 8). The per-sample cost is
still a single ``STA $D418``, so the NMI DAC handler is unchanged: only the byte
values written to the ring differ (0..255 instead of 0..15).

A "sidtable" here is the inverse map used at encode time: ``sidtable[i]`` is the
``$D418`` byte whose *measured* output level is nearest the i-th of 256 uniform
target levels spanning the SID's measured [min, max]. Index 128 is the neutral /
mid-scale entry (silence), the DAC analogue of the linear path's centered rest
value.

Why only the emulated (UltiSID) table ships baked
-------------------------------------------------
HW measurement (2026-07-02, Cam Link capture; ``scripts/diags/mahoney_dac_calib.py``):

* The U64's emulated **UltiSID** curve is deterministic across every unit and
  the 6581/8580 model knob does not affect the digi transfer (6581 vs 8580
  byte-identical, corr 0.99999) — so **one** baked ``mahoney_ultisid`` table
  generalises perfectly. Its curve is all-positive (~6.4 effective bits), a
  valid digi shape with silence at a mid-level code.
* **Physical 6581 chips vary enormously** chip-to-chip (two chips: curve corr
  0.738; one chip's table on the other → ~29% RMS level error), dominated by the
  analog filter. A single baked physical-6581 table cannot generalise, so
  physical chips get **per-unit calibration** instead of a shipped table (a
  deferred follow-up; see the project notes / ``--calibrate-dac`` sketch).

Credits: Pex 'Mahoney' Tufvesson (the technique + white paper §XIV env block),
Jürgen Wothke (websid effective-bit analysis), Antonio Savona / Broken Bytes
(the 48 kHz $D418 article), and CodeBase64.
"""

from __future__ import annotations

from typing import Final

# Emulated UltiSID amplitude(0..255) → $D418 byte, measured on the U64 FPGA
# UltiSID with the Mahoney SID env and "Digis Level = Medium" (the default).
# Deterministic across units; see scripts/diags/mahoney_measured_tables.json
# (ultisid.sidtable) for the source of record and the raw signed-level curve.
MAHONEY_ULTISID: Final[bytes] = bytes(
    (
        176,
        208,
        208,
        208,
        193,
        193,
        193,
        193,
        225,
        225,
        97,
        97,
        65,
        177,
        145,
        130,
        226,
        17,
        49,
        49,
        2,
        2,
        2,
        195,
        195,
        210,
        210,
        242,
        242,
        164,
        164,
        228,
        228,
        50,
        50,
        35,
        35,
        197,
        229,
        243,
        147,
        147,
        147,
        100,
        100,
        100,
        230,
        230,
        230,
        19,
        19,
        19,
        148,
        212,
        199,
        101,
        101,
        5,
        5,
        136,
        136,
        232,
        232,
        232,
        181,
        181,
        181,
        149,
        52,
        169,
        169,
        169,
        169,
        138,
        138,
        138,
        202,
        202,
        214,
        214,
        71,
        39,
        39,
        139,
        203,
        53,
        117,
        117,
        40,
        40,
        40,
        140,
        236,
        183,
        183,
        183,
        141,
        141,
        141,
        173,
        173,
        22,
        22,
        73,
        73,
        152,
        184,
        142,
        142,
        142,
        10,
        10,
        10,
        10,
        207,
        207,
        207,
        119,
        119,
        23,
        249,
        249,
        43,
        43,
        43,
        11,
        11,
        11,
        11,
        154,
        154,
        154,
        154,
        218,
        218,
        24,
        24,
        12,
        12,
        12,
        12,
        251,
        251,
        251,
        251,
        251,
        219,
        13,
        109,
        109,
        109,
        89,
        89,
        89,
        57,
        25,
        25,
        188,
        188,
        46,
        46,
        46,
        46,
        46,
        46,
        58,
        58,
        58,
        58,
        58,
        58,
        111,
        253,
        157,
        157,
        157,
        157,
        157,
        157,
        222,
        222,
        222,
        222,
        222,
        222,
        222,
        254,
        123,
        91,
        91,
        91,
        91,
        91,
        159,
        159,
        159,
        159,
        159,
        159,
        223,
        255,
        255,
        255,
        92,
        92,
        60,
        60,
        60,
        60,
        60,
        60,
        60,
        60,
        93,
        93,
        93,
        93,
        93,
        93,
        93,
        93,
        29,
        125,
        125,
        125,
        125,
        125,
        125,
        125,
        125,
        62,
        62,
        62,
        62,
        62,
        62,
        62,
        62,
        62,
        94,
        94,
        94,
        94,
        94,
        94,
        94,
        94,
        95,
        95,
        95,
        95,
        95,
        95,
        95,
        95,
        127,
    )
)

# Registry keyed by [audio].dac_curve value. "linear" is intentionally absent:
# it resolves to None (the legacy 4-bit path). New baked tables go here.
_DAC_CURVE_TABLES: Final[dict[str, bytes]] = {
    "mahoney_ultisid": MAHONEY_ULTISID,
}

# The neutral / mid-scale index shared by every companding table: encode maps a
# zero-amplitude (silence) sample to this amplitude index, and the ring is
# prefilled/padded with sidtable[NEUTRAL_INDEX].
NEUTRAL_INDEX: Final = 128

# Config choices for the introspection/schema layer (single source of truth).
# "auto" (default) and "calibrated" are *system-aware* choices resolved at
# runtime by dac_calibration.resolve_dac_curve_for_backend (they depend on the
# connected backend and whether a per-unit calibration exists); the baked-table
# names ("linear", "mahoney_ultisid") resolve here in resolve_dac_curve.
DAC_CURVE_CHOICES: Final[list[str]] = ["auto", "linear", *_DAC_CURVE_TABLES, "calibrated"]


def resolve_dac_curve(name: str) -> bytes | None:
    """Map a baked ``[audio].dac_curve`` name to its 256-entry amplitude→$D418
    table.

    Returns ``None`` for ``"linear"`` (the legacy 4-bit path, bit-identical to
    the pre-Mahoney encoder). Raises ``ValueError`` on an unknown name so a
    typo surfaces at config/construction time rather than silently falling back
    to linear. The system-aware ``"auto"``/``"calibrated"`` values are NOT baked
    tables — resolve them via
    :func:`c64cast.dac_calibration.resolve_dac_curve_for_backend` before calling
    this; passing them here raises.
    """
    if name == "linear":
        return None
    try:
        return _DAC_CURVE_TABLES[name]
    except KeyError:
        raise ValueError(
            f"unknown dac_curve {name!r}; choices: {', '.join(DAC_CURVE_CHOICES)}"
        ) from None
