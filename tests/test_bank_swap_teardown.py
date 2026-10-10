"""Bank-swap IRQ teardown: under a lossy link, and for a mode never set up.

`uninstall_bank_swap_irq` masks both IRQ sources, waits out an in-flight REU
copy, then restores `$0314`. A backend reports a lost write by moving
`delivery_epoch` rather than by raising, so an unchecked mask that never landed
passes as landed, and an IRQ can then enter the `$C500` dispatcher after the
drain and start a copy that the bank flip and the next scene's setup run under.

A bitmap mode's teardown unhooks only a handler its own setup hooked, so
tearing down a scene that was built but never set up (a cancelled performance
arm) leaves the scene on screen alone.
"""

from __future__ import annotations

import time
import unittest
from typing import Any, cast
from unittest import mock

from _fakes import FakeAPI

from c64cast.control.performance import ClipEvent, PerformanceSession
from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import CIA1, CIA2, KERNAL, VECTORS
from c64cast.hw.delivery import CONFIRM_TRIES
from c64cast.video import modes_irq
from c64cast.video.modes import HiresDisplayMode, MultiHiresDisplayMode
from c64cast.video.modes_irq import uninstall_bank_swap_irq

_CIA1_ICR = f"{CIA1.ICR:04X}"
_VECTOR = f"{VECTORS.IRQ:04X}"
_DD00 = f"{CIA2.PORT_A:04X}"
_DRAIN = f"sleep {modes_irq._REU_SLOT_MAX_IN_USE_S}"


class _LossyAPI(FakeAPI):
    """A FakeAPI that loses the first `losses[address]` writes to `address`,
    the way a lossy redial does: the call returns and the epoch moves. Every
    write and every drain is appended to `order`."""

    def __init__(self, losses: dict[str, int]):
        super().__init__()
        self.losses = dict(losses)
        self.order: list[str] = []

    def _maybe_lose(self, address: str) -> bool:
        address = address.upper()
        self.order.append(address)
        if self.losses.get(address, 0) > 0:
            self.losses[address] -= 1
            self.delivery_epoch += 1
            return True
        return False

    def write_memory(self, address, *args, **kwargs):
        if self._maybe_lose(address):
            return None
        return super().write_memory(address, *args, **kwargs)

    def write_regs(self, address, *args, **kwargs):
        if self._maybe_lose(address):
            return None
        return super().write_regs(address, *args, **kwargs)


def _teardown(api: _LossyAPI, *, drain_reu_copy: bool = True) -> None:
    with mock.patch.object(modes_irq, "time") as clock:
        clock.sleep.side_effect = lambda s: api.order.append(f"sleep {s}")
        uninstall_bank_swap_irq(cast(Ultimate64API, api), drain_reu_copy=drain_reu_copy)


class MaskRetryTest(unittest.TestCase):
    def test_a_mask_lost_once_is_written_again(self):
        for address in (_CIA1_ICR, "D01A"):
            with self.subTest(address=address):
                api = _LossyAPI({address: 1})
                _teardown(api)
                self.assertEqual(api.order.count(address), 2 if address == "D01A" else 3)
                self.assertEqual(api.order.count(_DRAIN), 1, "no second drain once it landed")
                self.assertEqual(
                    api.memories[_CIA1_ICR], f"{modes_irq._CIA1_ICR_ENABLE_TIMER_A:02X}"
                )

    def test_a_lost_vector_restore_is_written_again(self):
        api = _LossyAPI({_VECTOR: 1})
        _teardown(api)
        self.assertEqual(api.order.count(_VECTOR), 2)
        self.assertEqual(
            api.regs[_VECTOR], (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        )

    def test_a_lost_bank_0_write_is_written_again(self):
        api = _LossyAPI({_DD00: 1})
        _teardown(api)
        self.assertEqual(api.order.count(_DD00), 2)
        self.assertEqual(api.memories[_DD00], f"{modes_irq.DD00_BANK_0:02X}")

    def test_a_lost_cia1_unmask_is_written_again(self):
        # The mask and the unmask share an address, so lose only the unmask.
        api = _LossyAPI({})
        write_memory = api.write_memory
        unmask = f"{modes_irq._CIA1_ICR_ENABLE_TIMER_A:02X}"
        lost: list[str] = []

        def lose_first_unmask(address, value, *args, **kwargs):
            if address.upper() == _CIA1_ICR and value == unmask and not lost:
                lost.append(value)
                api.order.append(_CIA1_ICR)
                api.delivery_epoch += 1
                return None
            return write_memory(address, value, *args, **kwargs)

        api.write_memory = lose_first_unmask  # type: ignore[method-assign]
        _teardown(api)
        self.assertEqual(api.order.count(_CIA1_ICR), 3, "mask, lost unmask, unmask")
        self.assertEqual(api.memories[_CIA1_ICR], unmask)

    def test_an_unmask_that_never_confirms_is_logged(self):
        api = _LossyAPI({})
        write_memory = api.write_memory
        unmask = f"{modes_irq._CIA1_ICR_ENABLE_TIMER_A:02X}"

        def lose_every_unmask(address, value, *args, **kwargs):
            if address.upper() == _CIA1_ICR and value == unmask:
                api.delivery_epoch += 1
                return None
            return write_memory(address, value, *args, **kwargs)

        api.write_memory = lose_every_unmask  # type: ignore[method-assign]
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR") as logs:
            _teardown(api)
        self.assertIn("CIA1 unmask", "\n".join(logs.output))


class UnconfirmedMaskTest(unittest.TestCase):
    def test_an_unconfirmed_mask_drains_again_before_the_bank_is_released(self):
        for address in (_CIA1_ICR, "D01A"):
            with self.subTest(address=address):
                api = _LossyAPI({address: CONFIRM_TRIES})
                with self.assertLogs("c64cast.video.modes_irq", level="ERROR") as logs:
                    _teardown(api)
                self.assertTrue(any("not confirmed" in line for line in logs.output))
                drains = [i for i, op in enumerate(api.order) if op == _DRAIN]
                self.assertEqual(len(drains), 2)
                vector = api.order.index(_VECTOR)
                self.assertLess(vector, drains[1], "the second drain follows the restore")
                self.assertLess(drains[1], api.order.index(_DD00), "bank held until drained")

    def test_a_page_flip_with_no_copy_does_not_drain_again(self):
        api = _LossyAPI({"D01A": CONFIRM_TRIES})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api, drain_reu_copy=False)
        self.assertNotIn(_DRAIN, api.order)

    def test_an_unconfirmed_restore_leaves_cia1_masked(self):
        # The loss is silent, so this used to count as restored and re-arm
        # Timer A with $0314 still on the in-RAM handler.
        api = _LossyAPI({_VECTOR: CONFIRM_TRIES})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api)
        self.assertEqual(api.memories[_CIA1_ICR], f"{modes_irq._CIA1_ICR_DISABLE_TIMER_A:02X}")
        self.assertEqual(api.order.count(_DRAIN), 1, "no wait helps a handler left reachable")

    def test_a_mask_write_that_raises_counts_as_unconfirmed(self):
        api = _LossyAPI({})
        write_memory = api.write_memory

        def link_down_once(address, *args, **kwargs):
            if address.upper() == "D01A" and "D01A" not in api.order:
                api.order.append("D01A")
                raise RuntimeError("DMA link down")
            return write_memory(address, *args, **kwargs)

        api.write_memory = link_down_once  # type: ignore[method-assign]
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api)
        self.assertEqual(api.order.count(_DRAIN), 2)
        self.assertEqual(api.memories["D01A"], "00")


class MaskRetryBehindTheRestoreTest(unittest.TestCase):
    def test_an_unconfirmed_vic_mask_is_written_again_before_the_ack(self):
        # The kernal handler never acks $D019, so a raster source left live
        # behind the restore re-enters the IRQ on every RTI.
        api = _LossyAPI({"D01A": CONFIRM_TRIES + 1})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api)
        writes = [i for i, op in enumerate(api.order) if op == "D01A"]
        self.assertEqual(len(writes), CONFIRM_TRIES + 2)
        self.assertLess(api.order.index(_VECTOR), writes[-1])
        self.assertLess(writes[-1], api.order.index("D019"), "masked before the ack")
        self.assertEqual(api.memories["D01A"], "00")

    def test_a_restored_vector_skips_the_cia1_mask_retry(self):
        api = _LossyAPI({_CIA1_ICR: CONFIRM_TRIES})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api)
        self.assertEqual(api.order.count(_CIA1_ICR), CONFIRM_TRIES + 1, "only the unmask")
        self.assertEqual(api.memories[_CIA1_ICR], f"{modes_irq._CIA1_ICR_ENABLE_TIMER_A:02X}")

    def test_a_hooked_handler_gets_its_cia1_mask_written_again(self):
        api = _LossyAPI({_CIA1_ICR: CONFIRM_TRIES + 1, _VECTOR: CONFIRM_TRIES})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api)
        self.assertEqual(api.order.count(_CIA1_ICR), CONFIRM_TRIES + 2, "never unmasked")
        self.assertEqual(api.memories[_CIA1_ICR], f"{modes_irq._CIA1_ICR_DISABLE_TIMER_A:02X}")

    def test_a_hooked_handler_masked_on_retry_drains_before_the_bank_is_released(self):
        # The handler could start a copy until the retry landed, and with
        # every source masked after it nothing reaches $C500 again.
        for address in (_CIA1_ICR, "D01A"):
            with self.subTest(address=address):
                api = _LossyAPI({address: CONFIRM_TRIES + 1, _VECTOR: CONFIRM_TRIES})
                with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
                    _teardown(api)
                drains = [i for i, op in enumerate(api.order) if op == _DRAIN]
                self.assertEqual(len(drains), 2)
                last_mask = max(i for i, op in enumerate(api.order) if op == address)
                self.assertLess(last_mask, drains[1], "drained behind the retry")
                self.assertLess(drains[1], api.order.index(_DD00), "bank held until drained")

    def test_a_hooked_handler_still_reachable_gets_no_second_drain(self):
        api = _LossyAPI({_CIA1_ICR: 2 * CONFIRM_TRIES, _VECTOR: CONFIRM_TRIES})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api)
        self.assertEqual(api.order.count(_DRAIN), 1, "no wait helps a handler left reachable")

    def test_a_page_flip_masked_on_retry_does_not_drain_again(self):
        api = _LossyAPI({"D01A": CONFIRM_TRIES + 1, _VECTOR: CONFIRM_TRIES})
        with self.assertLogs("c64cast.video.modes_irq", level="ERROR"):
            _teardown(api, drain_reu_copy=False)
        self.assertNotIn(_DRAIN, api.order)


def _hooking_modes() -> list[HiresDisplayMode | MultiHiresDisplayMode]:
    return [
        mode
        for cls in (HiresDisplayMode, MultiHiresDisplayMode)
        for mode in (
            cls(use_reu_staged=True),
            cls(double_buffer=True),
            cls(flicker_tolerance="clean"),
        )
    ]


def _writes(api: FakeAPI) -> tuple[Any, ...]:
    return (dict(api.regs), dict(api.memories), dict(api.mem_files), api.cache_invalidations)


class NeverSetUpModeTest(unittest.TestCase):
    def test_teardown_without_setup_leaves_the_machine_alone(self):
        for mode in _hooking_modes():
            with self.subTest(mode=type(mode).__name__, reu=mode.use_reu_staged):
                api = FakeAPI()
                mode.teardown(cast(Ultimate64API, api))
                self.assertEqual(_writes(api), _writes(FakeAPI()))

    def test_teardown_after_setup_unhooks_once(self):
        kernal = (KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        for mode in _hooking_modes():
            with self.subTest(mode=type(mode).__name__, reu=mode.use_reu_staged):
                api = FakeAPI()
                with mock.patch.object(modes_irq, "time"), self.assertNoLogs(level="WARNING"):
                    mode.setup(cast(Ultimate64API, api))
                    mode.teardown(cast(Ultimate64API, api))
                self.assertEqual(api.regs[_VECTOR], kernal)
                before = _writes(api)
                mode.teardown(cast(Ultimate64API, api))
                self.assertEqual(_writes(api), before, "a second teardown unhooks nothing")


class _Scene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.is_done = False
        self.effects: list[Any] = []
        self.teardowns = 0

    def teardown(self) -> None:
        self.teardowns += 1


class _Playlist:
    """The surface PerformanceSession touches, with a beat grid that never
    reaches the arm's bar boundary."""

    def __init__(self) -> None:
        self.tempo = mock.Mock(beat_phase=0.0, bar_phase=0.0, running=True)
        self.current = _Scene("on screen")
        self.scenes = [self.current]
        self.built: list[_Scene] = []
        self.build_performance_scene = self._build

    def _build(self, clip: dict[str, Any]) -> _Scene:
        scene = _Scene(f"clip{clip['slot']}")
        self.built.append(scene)
        return scene

    def perf_swap_scene(self, new_scene: _Scene) -> bool:
        raise AssertionError("the bar boundary never arrives")


class CancelledArmTest(unittest.TestCase):
    def _wait_built(self, session: PerformanceSession, pl: Any, count: int) -> None:
        deadline = time.monotonic() + 5
        while len(pl.built) < count and time.monotonic() < deadline:
            session.service(pl)
            time.sleep(0.005)
        self.assertEqual(len(pl.built), count)
        # The factory returns before the build thread sets `built`, and a
        # cancel that lands in between skipped the teardown even before the fix.
        armed = session._armed
        assert armed is not None
        self.assertTrue(armed.built.wait(5))

    def test_a_superseded_arm_is_dropped_without_a_teardown(self):
        pl: Any = _Playlist()
        session = PerformanceSession(
            [{"slot": 1, "type": "generative"}, {"slot": 2, "type": "generative"}]
        )
        session.enqueue(ClipEvent(slot=1, pressed=True))
        self._wait_built(session, pl, 1)
        session.enqueue(ClipEvent(slot=2, pressed=True))
        self._wait_built(session, pl, 2)
        self.assertEqual(session.armed_slot, 2)
        self.assertEqual(pl.built[0].teardowns, 0)
        self.assertEqual(pl.current.teardowns, 0)

    def test_a_released_gate_arm_is_dropped_without_a_teardown(self):
        pl: Any = _Playlist()
        session = PerformanceSession([{"slot": 1, "type": "generative", "launch": "gate"}])
        session.enqueue(ClipEvent(slot=1, pressed=True))
        self._wait_built(session, pl, 1)
        session.enqueue(ClipEvent(slot=1, pressed=False))
        session.service(pl)
        self.assertIsNone(session.armed_slot)
        self.assertEqual(pl.built[0].teardowns, 0)


if __name__ == "__main__":
    unittest.main()
