"""Raster IRQ installs mask both IRQ sources before they upload anything.

A teardown whose `$0314` restore never landed leaves the vector on the old
handler's address. An install that uploads over that address while CIA #1 or
the raster source is still live lets an IRQ run a half-written handler.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any, cast
from unittest import mock

from _fakes import FakeAPI, quiet_logging

from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import CIA1, CIA2, VECTORS, VIC_BANK_2
from c64cast.scenes.overlays.big_text import BigTextOverlay
from c64cast.video import modes_irq
from c64cast.video.modes import HiresDisplayMode, MultiHiresDisplayMode

_CIA1_ICR = f"{CIA1.ICR:04X}"
_VECTOR = f"{VECTORS.IRQ:04X}"


def _first(ops: list[tuple[Any, ...]], name: str, address: str, value: Any = None) -> int:
    for i, op in enumerate(ops):
        if op[0] == name and op[1] == address and (value is None or op[2] == value):
            return i
    raise AssertionError(f"no {name} to {address}")


def _label(mode: HiresDisplayMode | MultiHiresDisplayMode) -> str:
    if mode.use_reu_staged:
        path = "reu"
    elif mode.double_buffer:
        path = "double_buffer"
    else:
        path = "flicker"
    return f"{type(mode).__name__}/{path}"


class InstallOrderTest(unittest.TestCase):
    def _assert_masked_before_uploads(
        self, ops: list[tuple[Any, ...]], mask_value: str, *, unmasks: bool
    ) -> None:
        mask = _first(ops, "write_memory", _CIA1_ICR, mask_value)
        disable = _first(ops, "write_memory", "D01A", "00")
        uploads = [i for i, op in enumerate(ops) if op[0] in ("write_memory_file", "write_regs")]
        self.assertTrue(uploads)
        self.assertLess(max(mask, disable), min(uploads), "every upload follows both masks")
        hook = _first(ops, "write_regs", _VECTOR)
        self.assertLess(max(uploads[:-1]), hook, "hooked last")
        self.assertLess(hook, _first(ops, "write_memory", "D01A", "01"), "armed after the hook")
        if unmasks:
            self.assertLess(hook, _first(ops, "write_memory", _CIA1_ICR, "81"), "re-armed last")

    def test_bank_swap_install_masks_before_it_uploads(self):
        for pump in (False, True):
            with self.subTest(audio_pump_active=pump):
                api = FakeAPI()
                modes_irq.install_bank_swap_irq(
                    cast(Ultimate64API, api),
                    modes_irq.BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER
                    if pump
                    else modes_irq.BANK_SWAP_IRQ_HANDLER,
                    audio_pump_active=pump,
                )
                self._assert_masked_before_uploads(
                    api.ops, f"{modes_irq._CIA1_ICR_DISABLE_TIMER_A:02X}", unmasks=True
                )

    def test_a_bad_tracker_init_raises_before_any_write(self):
        api = FakeAPI()
        with self.assertRaises(ValueError):
            modes_irq.install_bank_swap_irq(cast(Ultimate64API, api), tracker_init=b"\x00")
        self.assertEqual(api.ops, [])

    def test_double_buffer_setups_mask_and_drain_before_they_clear_or_pin(self):
        for mode in (
            HiresDisplayMode(use_reu_staged=True),
            HiresDisplayMode(double_buffer=True),
            HiresDisplayMode(flicker_tolerance="clean"),
            MultiHiresDisplayMode(use_reu_staged=True),
            MultiHiresDisplayMode(double_buffer=True),
            MultiHiresDisplayMode(flicker_tolerance="clean"),
        ):
            with self.subTest(mode=_label(mode)):
                api = FakeAPI()
                with mock.patch.object(modes_irq, "time") as clock:
                    clock.sleep.side_effect = lambda s, ops=api.ops: ops.append(("sleep", s))
                    with quiet_logging():
                        mode.setup(cast(Ultimate64API, api))
                ops = api.ops
                drain = ops.index(("sleep", modes_irq._REU_SLOT_MAX_IN_USE_S))
                mask = _first(ops, "write_memory", _CIA1_ICR, "7F")
                disable = _first(ops, "write_memory", "D01A", "00")
                pin = _first(ops, "write_memory", f"{CIA2.PORT_A:04X}")
                clear = _first(ops, "write_memory_file", f"{VIC_BANK_2.BITMAP:04X}")
                engage = [_first(ops, "write_memory", reg) for reg in ("D018", "D016", "D011")] + [
                    _first(ops, "write_regs", reg) for reg in ("D020", "D021")
                ]
                self.assertLess(max(mask, disable), drain)
                self.assertLess(drain, min(pin, clear, *engage))

    def test_a_single_buffer_setup_leaves_the_irq_sources_alone(self):
        # Nothing in a single-buffer scene would ever re-arm CIA #1.
        for mode in (HiresDisplayMode(), MultiHiresDisplayMode()):
            with self.subTest(mode=type(mode).__name__):
                api = FakeAPI()
                with quiet_logging():
                    mode.setup(cast(Ultimate64API, api))
                    mode.teardown(cast(Ultimate64API, api))
                self.assertNotIn(("write_memory", _CIA1_ICR, "7F"), api.ops)
                self.assertNotIn(("write_memory", "D01A", "00"), api.ops)

    def test_a_double_buffer_setup_on_a_backend_with_no_reu_skips_the_drain(self):
        for mode in (
            HiresDisplayMode(double_buffer=True),
            HiresDisplayMode(flicker_tolerance="clean"),
            MultiHiresDisplayMode(double_buffer=True),
            MultiHiresDisplayMode(flicker_tolerance="clean"),
        ):
            with self.subTest(mode=_label(mode)):
                api = FakeAPI()
                api.profile = replace(api.profile, supports_reu=False)
                with mock.patch.object(modes_irq, "time") as clock:
                    with quiet_logging():
                        mode.setup(cast(Ultimate64API, api))
                clock.sleep.assert_not_called()
                _first(api.ops, "write_memory", _CIA1_ICR, "7F")

    def test_a_setup_cut_short_after_the_mask_still_unmasks_on_teardown(self):
        class _LinkCut(FakeAPI):
            def write_memory_file(self, address, data):
                raise ConnectionError("link down")

        for mode in (
            HiresDisplayMode(use_reu_staged=True),
            HiresDisplayMode(double_buffer=True),
            MultiHiresDisplayMode(flicker_tolerance="clean"),
        ):
            with self.subTest(mode=type(mode).__name__, reu=mode.use_reu_staged):
                api = _LinkCut()
                with mock.patch.object(modes_irq, "time"):
                    with quiet_logging():
                        with self.assertRaises(ConnectionError):
                            mode.setup(cast(Ultimate64API, api))
                        mode.teardown(cast(Ultimate64API, api))
                mask = _first(api.ops, "write_memory", _CIA1_ICR, "7F")
                unmask = _first(api.ops, "write_memory", _CIA1_ICR, "81")
                self.assertLess(mask, unmask)

    def test_big_text_install_masks_before_it_uploads(self):
        api = FakeAPI()
        overlay: Any = BigTextOverlay(messages=[{"text": "HI"}], charset_path="")
        overlay._install_raster_irq(api)
        # big_text keeps CIA #1 masked while hooked; its handler chains to $EA31.
        self._assert_masked_before_uploads(api.ops, "7F", unmasks=False)


if __name__ == "__main__":
    unittest.main()
