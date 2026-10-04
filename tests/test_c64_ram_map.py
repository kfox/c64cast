"""The fixed C64 RAM regions in `$C000-$CFFF`, held disjoint wherever two of
them can be live at once.

Every module that puts 6502 code or data at a fixed address in that 4 KB picks
the address on its own, so nothing but this test sees two of them at once.
big_text kept its shadow `$D016`/`$D018` bytes at `$C100`/`$C101`, the REU
audio pump's IRQ entry, and every frame overwrote the pump's first two opcodes
(#559). Two halves close that class:

* `CoResidencyTest` builds each region from the real constants and the real
  byte lengths, and fails when two owners that can be live in the same scene
  overlap. Owners that never run together are listed in `_NEVER_LIVE_TOGETHER`
  with the reason; anything not listed is assumed to share a scene, so a new
  pairing fails until someone says why it is safe.
* `SweepTest` reads every module under `c64cast/` and fails on a module-level
  integer constant in `$C000-$CFFF` that `_REGIONS` does not map, so the next
  fixed address cannot skip the first half.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
import unittest
from dataclasses import dataclass

from c64cast.audio import audio_handlers as ah
from c64cast.hw import api
from c64cast.hw import teensyrom_api as tr
from c64cast.scenes.overlays import big_text
from c64cast.sid import asid_player as ap
from c64cast.video import modes_irq as mi

_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "c64cast"
_SWEEP_LO = 0xC000
_SWEEP_HI = 0xD000  # exclusive


@dataclass(frozen=True)
class Region:
    owner: str
    label: str
    start: int
    length: int
    #: "module:NAME" of the constant that places the region, for the sweep.
    const: str

    @property
    def end(self) -> int:
        return self.start + self.length

    def __str__(self) -> str:
        return f"{self.owner} {self.label} ${self.start:04X}-${self.end - 1:04X}"


def _longest(*blobs: bytes) -> int:
    return max(len(b) for b in blobs)


_AH = "c64cast.audio.audio_handlers"
_MI = "c64cast.video.modes_irq"
_API = "c64cast.hw.api"
_AP = "c64cast.sid.asid_player"

_REGIONS: tuple[Region, ...] = (
    Region(
        "big_text",
        "raster IRQ handler",
        big_text.IRQ_HANDLER_ADDR,
        len(big_text.RASTER_IRQ_HANDLER),
        "c64cast.scenes.overlays.big_text:IRQ_HANDLER_ADDR",
    ),
    Region(
        "big_text",
        "shadow $D016",
        big_text.SHADOW_D016_ADDR,
        1,
        "c64cast.scenes.overlays.big_text:SHADOW_D016_ADDR",
    ),
    Region(
        "big_text",
        "shadow $D018",
        big_text.SHADOW_D018_ADDR,
        1,
        "c64cast.scenes.overlays.big_text:SHADOW_D018_ADDR",
    ),
    Region(
        "dac_nmi",
        "NMI DAC routine",
        ah.NMI_ROUTINE_ADDR,
        len(ah.NMI_ROUTINE),
        f"{_AH}:NMI_ROUTINE_ADDR",
    ),
    Region(
        "reu_pump",
        "IRQ entry (every variant)",
        ah.REU_PUMP_HANDLER_ADDR,
        _longest(
            ah.REU_IRQ_HANDLER,
            ah.REU_IRQ_HANDLER_GOVERNOR,
            ah.REU_IRQ_HANDLER_TRACKED,
            ah.REU_MIC_IRQ_HANDLER,
        ),
        f"{_AH}:REU_PUMP_HANDLER_ADDR",
    ),
    Region(
        "reu_pump",
        "JMP $EA31 placeholder the bank-swap installer writes before the pump",
        mi.AUDIO_HANDLER_INSTALL_ADDR,
        len(mi.AUDIO_HANDLER_STUB),
        f"{_MI}:AUDIO_HANDLER_INSTALL_ADDR",
    ),
    Region(
        "reu_pump",
        "pump-body subroutine",
        ah.REU_PUMP_BODY_SUBROUTINE_ADDR,
        _longest(ah.REU_PUMP_BODY_SUBROUTINE, ah.REU_PUMP_BODY_SUBROUTINE_GOVERNOR),
        f"{_AH}:REU_PUMP_BODY_SUBROUTINE_ADDR",
    ),
    Region(
        "reu_pump",
        "src/dst tracker",
        ah.REU_AUDIO_SRC_TRACKER_ADDR,
        5,
        f"{_AH}:REU_AUDIO_SRC_TRACKER_ADDR",
    ),
    Region(
        "reu_pump",
        "kernal-tail tick counter",
        ah.REU_PUMP_TICK_COUNTER_ADDR,
        1,
        f"{_AH}:REU_PUMP_TICK_COUNTER_ADDR",
    ),
    Region(
        "sid_player",
        "player MC (default base)",
        api.SID_PLAYER_MC_ADDR,
        len(api.SID_PLAYER_MC_TEMPLATE),
        f"{_API}:SID_PLAYER_MC_ADDR",
    ),
    Region(
        "sid_player",
        "re-INIT stub (default base)",
        api.REINIT_STUB_ADDR,
        len(api.REINIT_STUB_TEMPLATE),
        f"{_API}:REINIT_STUB_ADDR",
    ),
    Region(
        "bank_swap",
        "raster IRQ handler (every variant)",
        mi.BANK_SWAP_IRQ_HANDLER_ADDR,
        _longest(
            mi.BANK_SWAP_IRQ_HANDLER,
            mi.MHIRES_BANK_SWAP_IRQ_HANDLER,
            mi.BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER,
            mi.MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER,
            mi.MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
            mi.HOSTDMA_SWAP_IRQ_HANDLER,
            mi.FLICKER_SWAP_IRQ_HANDLER,
        ),
        f"{_MI}:BANK_SWAP_IRQ_HANDLER_ADDR",
    ),
    Region(
        "bank_swap",
        "frame tracker (every layout)",
        mi.FRAME_TRACKER_ADDR,
        max(
            mi.FRAME_TRACKER_LEN,
            mi.MHIRES_FRAME_TRACKER_LEN,
            mi.HOSTDMA_TRACKER_LEN,
            mi.FLICKER_TRACKER_LEN,
        ),
        f"{_MI}:FRAME_TRACKER_ADDR",
    ),
    Region(
        "asid",
        "player IRQ handler (8-SID worst case)",
        ap.HANDLER_ADDR,
        len(ap.build_player(ap.slot_size_for_chips(8), 16)),
        f"{_AP}:HANDLER_ADDR",
    ),
    Region(
        "asid",
        "REU landing buffer (8-SID worst case)",
        ap.LANDING_BUF,
        ap.slot_size_for_chips(8),
        f"{_AP}:LANDING_BUF",
    ),
    Region("asid", "src tracker", ap.TRACKER_ADDR, 3, f"{_AP}:TRACKER_ADDR"),
    Region("asid", "tick counter", ap.TICK_COUNTER_ADDR, 1, f"{_AP}:TICK_COUNTER_ADDR"),
    Region("asid", "op-loop counter", ap.NOPS_COUNTER_ADDR, 1, f"{_AP}:NOPS_COUNTER_ADDR"),
    Region(
        "char_rom_dump",
        "4 KB landing zone",
        api.CHAR_ROM_DUMP_DEST,
        api.CHAR_ROM_DUMP_BYTES,
        f"{_API}:CHAR_ROM_DUMP_DEST",
    ),
    Region(
        "tr_spin",
        "old-firmware TeensyROM idle sled",
        tr._SPIN_STUB_ADDR,
        len(tr._SPIN_STUB),
        "c64cast.hw.teensyrom_api:_SPIN_STUB_ADDR",
    ),
)

#: Constants in the sweep range that place nothing: bounds other code tests
#: against.
_NOT_A_REGION: dict[str, str] = {
    f"{_API}:_AUDIO_REGION_LO": "lower bound the SID-player relocator keeps clear",
    f"{_API}:_AUDIO_REGION_HI": "upper bound the SID-player relocator keeps clear",
}

#: Owner pairs that overlap but are never live in the same scene, and why.
#: Live means the code can execute or the data can be read: a handler left in
#: RAM after its interrupt source is off is not live. Every entry must have an
#: overlap behind it, so a claim nothing depends on fails instead of lingering.
_NEVER_LIVE_TOGETHER: dict[frozenset[str], str] = {
    **{
        frozenset({"asid", other}): (
            "AsidScene builds no display mode (so no bank swap, and validation "
            "checks its overlays against hires, which big_text refuses), starts "
            "no DAC streamer, and plays no SID file"
        )
        for other in ("big_text", "dac_nmi", "sid_player", "bank_swap")
    },
    frozenset({"asid", "tr_spin"}): "the ASID player needs the REU, which no TeensyROM has",
    **{
        frozenset({"char_rom_dump", other}): (
            "the dump runs before any scene (--dump-char-rom, or the first-run "
            "char_rom.ensure_installed) and invalidates the write cache, so the "
            "next scene uploads its handlers fresh"
        )
        for other in ("big_text", "dac_nmi", "reu_pump", "sid_player", "bank_swap", "asid")
    },
    frozenset({"char_rom_dump", "tr_spin"}): (
        "the dump needs read_memory, which the spin-stub firmware lacks"
    ),
}

#: Overlaps between owners that can be live together and are known to be
#: broken, each with the issue tracking it. An entry with no overlap behind it
#: fails, so the fix has to delete its row here.
_KNOWN_COLLISIONS: dict[frozenset[str], str] = {
    frozenset({"tr_spin", "big_text"}): "#561",
    frozenset({"tr_spin", "dac_nmi"}): "#561",
}


def _overlapping_pairs() -> list[tuple[Region, Region]]:
    """Every pair of regions with different owners that share a byte."""
    return [
        (a, b)
        for a, b in itertools.combinations(_REGIONS, 2)
        if a.owner != b.owner and a.start < b.end and b.start < a.end
    ]


class CoResidencyTest(unittest.TestCase):
    def test_owners_that_can_be_live_together_do_not_overlap(self):
        unexpected = [
            f"{a} overlaps {b}"
            for a, b in _overlapping_pairs()
            if frozenset({a.owner, b.owner}) not in {*_NEVER_LIVE_TOGETHER, *_KNOWN_COLLISIONS}
        ]
        self.assertEqual(unexpected, [], "fixed RAM regions that can be live together overlap")

    def test_every_listed_pair_has_an_overlap_behind_it(self):
        overlapping = {frozenset({a.owner, b.owner}) for a, b in _overlapping_pairs()}
        listed = set(_NEVER_LIVE_TOGETHER) | set(_KNOWN_COLLISIONS)
        self.assertEqual(sorted(sorted(p) for p in listed - overlapping), [])

    def test_every_exclusion_names_owners_that_exist(self):
        owners = {r.owner for r in _REGIONS}
        for pair in (*_NEVER_LIVE_TOGETHER, *_KNOWN_COLLISIONS):
            with self.subTest(pair=sorted(pair)):
                self.assertLessEqual(pair, owners)

    def test_regions_stay_inside_the_sweep_range(self):
        for r in _REGIONS:
            with self.subTest(region=str(r)):
                self.assertGreaterEqual(r.start, _SWEEP_LO)
                self.assertLessEqual(r.end, _SWEEP_HI)

    def test_big_text_shares_no_byte_with_the_audio_handlers(self):
        # The #559 instance by literal address: the symbolic check above moves
        # with the constants, so it alone cannot say the shadows left $C100.
        self.assertEqual((big_text.SHADOW_D016_ADDR, big_text.SHADOW_D018_ADDR), (0xC01E, 0xC01F))
        handler = big_text.RASTER_IRQ_HANDLER
        self.assertEqual(handler[1:3], bytes([0x1E, 0xC0]))  # LDA $C01E
        self.assertEqual(handler[7:9], bytes([0x1F, 0xC0]))  # LDA $C01F


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(_PACKAGE.parent).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


def _swept_constants() -> dict[str, int]:
    """Every module-level `NAME = 0x<hex literal>` (annotated or not) under
    c64cast/ whose value lands in the sweep range. Hex only: the package writes
    addresses in hex, and a decimal count such as a cycle budget can land in the
    range by coincidence."""
    found: dict[str, int] = {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not (isinstance(value, ast.Constant) and type(value.value) is int):
                continue
            if not _SWEEP_LO <= value.value < _SWEEP_HI:
                continue
            literal = ast.get_source_segment(source, value) or ""
            if not literal.lower().startswith("0x"):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    found[f"{_module_name(path)}:{target.id}"] = value.value
    return found


class SweepTest(unittest.TestCase):
    def test_every_fixed_address_in_the_range_is_mapped(self):
        mapped = {r.const for r in _REGIONS} | set(_NOT_A_REGION)
        unmapped = sorted(set(_swept_constants()) - mapped)
        self.assertEqual(
            unmapped,
            [],
            "module-level constants in $C000-$CFFF with no entry in this test's "
            "_REGIONS (or _NOT_A_REGION)",
        )

    def test_every_mapped_constant_still_exists(self):
        swept = _swept_constants()
        for const in sorted({r.const for r in _REGIONS} | set(_NOT_A_REGION)):
            with self.subTest(const=const):
                self.assertIn(const, swept)

    def test_the_sweep_sees_a_known_constant(self):
        # Guards the sweep itself: a parse that found nothing would pass both
        # tests above vacuously.
        self.assertEqual(
            _swept_constants().get("c64cast.scenes.overlays.big_text:SHADOW_D016_ADDR"), 0xC01E
        )


if __name__ == "__main__":
    unittest.main()
