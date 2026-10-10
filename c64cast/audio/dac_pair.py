"""The two-SID ``$D418`` DAC: a coarse chip at ``$D400`` and a fine chip at a
lower mixer level, played as one higher-resolution DAC (c64cast#590).

Pure data + pure functions: the pair NMI routine, where its two lookup tables
live, the fold from two measured ladders to those tables, and the parse of
``[audio].dac_second_sid``. Nothing here touches hardware.

**The ring is unchanged.** Each ring byte is still one 8-bit amplitude index;
the pair routine looks it up in two 256-byte tables on the C64 and writes the
coarse chip's ``$D418`` and the fine chip's. So every producer — host DMA, the
REU pump, the offline pre-encode — and the read pointer the servos and pump
governors read are exactly what the one-chip path has. The encoder plays the
pair through the identity curve (:data:`IDENTITY_TABLE`).

**The fine chip plays its volume nibble only.** Measured on hardware, letting
the fine chip use its Mahoney filter-mode codes as well scores far more
reachable levels and plays worse (13.99 dB against 22.44 dB SNDR at −30 dBFS):
those codes switch its filter routing, and at a 1/16 mixer level the volume
ladder is already as fine as the coarse ladder's gaps need.

See docs/architecture/audio.md#dac_pairpy--two-sid-d418-dac.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from .audio_handlers import (
    NMI_ROUTINE,
    NMI_ROUTINE_ADDR,
    READ_PTR_HI_ADDR,
    READ_PTR_LO_ADDR,
)

#: Index → coarse-chip ``$D418`` byte, and index → fine-chip byte. Page-aligned
#: so ``LDA table`` never pays a page-cross cycle, in the `$C000` page above
#: everything else c64cast places there (tests/test_c64_ram_map.py).
COARSE_TABLE_ADDR: Final = 0xCE00
FINE_TABLE_ADDR: Final = 0xCF00

#: Codes the fine chip may play: the volume nibble, filter-mode bits clear.
FINE_CODES: Final = tuple(range(16))

#: The fine chip's Ultimate mixer level, relative to the coarse chip's unity.
#: −24 dB is ≈1/16: the fine chip's whole volume ladder then spans one step of
#: the coarse chip's. Measured against −18, −27 and −30 dB, it was best or
#: within 1 dB of best at every playback level.
FINE_GAIN_DB: Final = -24

IDENTITY_TABLE: Final = bytes(range(256))

# One window of $20-aligned SID bases the Ultimate can map a SID to, plus the
# cartridge-port page; $DF00+ is the REU and the sampler.
_FINE_BASES: Final = frozenset([*range(0xD420, 0xD800, 0x20), *range(0xDE00, 0xDF00, 0x20)])

_BASE_SPELLING: Final = re.compile(r"(?:\$|0x)?([0-9a-f]{4})")


def _check_fine_base(fine_base: int) -> None:
    if fine_base not in _FINE_BASES:
        raise ValueError(f"no second SID can sit at ${fine_base:04X}")


def pair_nmi_routine(fine_base: int) -> bytes:
    """:data:`NMI_ROUTINE` with its ``STA $D418`` replaced by the two lookups.

    Disassembly at ``$C020`` (fast path 61 cycles against the one-chip 41)::

        $C020: 48           PHA
        $C021: AD 0D DD     LDA $DD0D            ; ack CIA #2 NMI
        $C024: AD 00 40     LDA R                ; read the index (operand = R)
        $C027: 8D 2E C0     STA $C02E            ; → coarse lookup operand LO
        $C02A: 8D 34 C0     STA $C034            ; → fine lookup operand LO
        $C02D: AD 00 CE     LDA COARSE_TABLE
        $C030: 8D 18 D4     STA $D418
        $C033: AD 00 CF     LDA FINE_TABLE
        $C036: 8D xx xx     STA fine_base+$18
        $C039: ...          NMI_ROUTINE's INC / wrap tail, unchanged

    Self-modifying operands rather than ``TAX`` + indexed loads, so X and Y
    stay untouched like the one-chip routine's, at six cycles over a ``TAX``
    that clobbers X."""
    _check_fine_base(fine_base)
    head, tail = NMI_ROUTINE[:7], NMI_ROUTINE[10:]
    coarse_op = NMI_ROUTINE_ADDR + len(head) + 6 + 1
    fine_op = coarse_op + 6
    fine_d418 = fine_base + 0x18
    middle = bytes(
        [
            0x8D, coarse_op & 0xFF, coarse_op >> 8,  # STA coarse op LO
            0x8D, fine_op & 0xFF, fine_op >> 8,  # STA fine op LO
            0xAD, 0x00, COARSE_TABLE_ADDR >> 8,  # LDA COARSE_TABLE
            0x8D, 0x18, 0xD4,  # STA $D418
            0xAD, 0x00, FINE_TABLE_ADDR >> 8,  # LDA FINE_TABLE
            0x8D, fine_d418 & 0xFF, fine_d418 >> 8,  # STA fine $D418
        ]
    )  # fmt: skip
    routine = head + middle + tail
    assert routine[4] == 0xAD and NMI_ROUTINE_ADDR + 5 == READ_PTR_LO_ADDR
    assert READ_PTR_HI_ADDR == READ_PTR_LO_ADDR + 1
    return routine


def parse_second_sid(value: str) -> int | None:
    """``[audio].dac_second_sid`` → the fine chip's base, or None for ``"off"``.
    Raises ``ValueError`` naming what is accepted."""
    text = value.strip().lower()
    if text == "off":
        return None
    spelled = _BASE_SPELLING.fullmatch(text)
    base = int(spelled.group(1), 16) if spelled else -1
    if base not in _FINE_BASES:
        raise ValueError(
            f'[audio].dac_second_sid = {value!r}: expected "off" or a SID base '
            "from $D420 to $D7E0 or $DE00 to $DEE0, on a $20 boundary"
        )
    return base


@dataclass(frozen=True)
class DacPair:
    """What a two-SID run uploads: the fine chip's base and the two tables."""

    fine_base: int
    coarse_table: bytes
    fine_table: bytes

    def __post_init__(self) -> None:
        _check_fine_base(self.fine_base)
        if len(self.coarse_table) != 256 or len(self.fine_table) != 256:
            raise ValueError("a DAC pair needs two 256-entry tables")
        if any(b not in FINE_CODES for b in self.fine_table):
            raise ValueError("the fine table may hold volume-nibble codes only")


def fold_pair_table(
    coarse_levels: np.ndarray, fine_levels: np.ndarray
) -> tuple[list[int], list[int], dict[str, Any]]:
    """The 256 uniform targets across the pair's span, each mapped to the
    ``(coarse, fine)`` codes whose summed level is nearest.

    ``coarse_levels`` is the coarse chip's 256 signed levels, ``fine_levels``
    the fine chip's :data:`FINE_CODES` levels, in the same units (both measured
    against one coarse anchor). The span is the coarse ladder's own, not the
    sum's: the fine chip fills in between coarse steps, and stretching the
    targets over its extra reach would spend index steps on two thin slivers
    at the ends. Returns the two tables and the same ladder metrics a one-chip
    calibration reports, plus ``single_chip_ladder_bits`` for comparison."""
    from .dac_slot_ring import _ladder_metrics

    coarse = np.asarray(coarse_levels, dtype=np.float64)
    fine = np.asarray(fine_levels, dtype=np.float64)
    if coarse.shape != (256,) or fine.shape != (len(FINE_CODES),):
        raise ValueError("expected 256 coarse levels and 16 fine levels")
    lo, hi = float(coarse.min()), float(coarse.max())
    span = hi - lo
    targets = np.linspace(lo, hi, 256)
    sums = (coarse[:, None] + fine[None, :]).ravel()
    pick = np.argmin(np.abs(sums[None, :] - targets[:, None]), axis=1)
    coarse_table = [int(p // fine.size) for p in pick]
    fine_table = [int(FINE_CODES[p % fine.size]) for p in pick]
    single = coarse[np.argmin(np.abs(coarse[None, :] - targets[:, None]), axis=1)]
    metrics = {
        **_ladder_metrics(sums[pick], targets, span),
        "single_chip_ladder_bits": _ladder_metrics(single, targets, span)["ladder_bits"],
    }
    return coarse_table, fine_table, metrics
