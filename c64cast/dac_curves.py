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
mid-scale entry (silence), the DAC analog of the linear path's centered rest
value.

Why only the emulated (UltiSID) table ships baked
-------------------------------------------------
HW measurement (2026-08-06, Cam Link capture;
``scripts/diags/mahoney_slot_ring_probe.py --source ultisid1``, 18 rings merged
over two runs). Supersedes the 2026-07-02 measurement, whose *curve* it
reproduces at corr 0.99957 — the emulated core really was what that pass
captured — but whose *table* it does not: the levels→code fold has been
rewritten since, and the old bytes are non-monotonic through the curve they
came from (27 backward steps, worst −1.73% of span, against 0 for these).

* The U64's emulated **UltiSID** curve is deterministic across every unit and
  the 6581/8580 model knob does not affect the digi transfer (6581 vs 8580
  byte-identical, corr 0.99999) — so **one** baked ``mahoney_ultisid`` table
  generalizes perfectly. Its curve is all-positive, a valid digi shape with
  silence at a mid-level code.
* Measuring it needs the core **isolated and unmuted**. Address routing alone
  is not enough: the Audio Mixer carries an independent per-source level, and a
  rig that has only ever used socketed chips ships ``Vol UltiSid 1 = OFF``. That
  yields a capture at the noise floor, which is indistinguishable from a bring-up
  failure rather than obviously being a routing one.
* **Physical 6581 chips vary enormously** chip-to-chip (two chips: curve corr
  0.738; one chip's table on the other → ~29% RMS level error), dominated by the
  analog filter. A single baked physical-6581 table cannot generalize, so
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
        0,
        64,
        64,
        64,
        64,
        161,
        161,
        161,
        161,
        193,
        1,
        1,
        33,
        209,
        241,
        194,
        162,
        17,
        49,
        49,
        163,
        195,
        2,
        98,
        34,
        34,
        146,
        146,
        178,
        164,
        164,
        67,
        67,
        3,
        3,
        50,
        82,
        165,
        179,
        243,
        198,
        198,
        198,
        68,
        68,
        4,
        199,
        199,
        231,
        83,
        19,
        180,
        244,
        244,
        200,
        200,
        69,
        69,
        69,
        69,
        38,
        38,
        38,
        6,
        169,
        245,
        181,
        116,
        52,
        52,
        234,
        234,
        170,
        170,
        182,
        7,
        7,
        7,
        203,
        203,
        171,
        171,
        85,
        85,
        85,
        21,
        104,
        8,
        204,
        172,
        172,
        172,
        172,
        141,
        141,
        141,
        205,
        54,
        41,
        73,
        248,
        248,
        184,
        206,
        238,
        174,
        174,
        174,
        74,
        74,
        106,
        239,
        207,
        207,
        119,
        185,
        87,
        87,
        87,
        107,
        107,
        107,
        43,
        43,
        43,
        43,
        218,
        218,
        218,
        154,
        250,
        56,
        56,
        88,
        76,
        76,
        76,
        76,
        76,
        251,
        251,
        251,
        251,
        187,
        77,
        77,
        109,
        109,
        57,
        57,
        25,
        89,
        89,
        89,
        188,
        188,
        188,
        14,
        46,
        46,
        46,
        46,
        46,
        122,
        122,
        122,
        122,
        122,
        47,
        79,
        221,
        157,
        157,
        157,
        157,
        157,
        157,
        157,
        254,
        254,
        254,
        254,
        254,
        254,
        190,
        91,
        91,
        91,
        91,
        91,
        91,
        91,
        191,
        191,
        191,
        191,
        191,
        191,
        223,
        223,
        92,
        92,
        92,
        124,
        124,
        124,
        124,
        124,
        124,
        124,
        124,
        124,
        125,
        125,
        125,
        125,
        125,
        125,
        125,
        125,
        61,
        61,
        61,
        61,
        61,
        61,
        61,
        61,
        61,
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
