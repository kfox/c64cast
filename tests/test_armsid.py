"""Tests for ARMSID / ARM2SID support (c64cast/sid/armsid.py) and the planners
that consult it: the register protocol against a fake chip, socket labels, the
right channel behind the Ext DualSID split, and the model switch riding the
same plan / snapshot / restore as the REST items."""

# FakeAPI duck-types C64Backend rather than subclassing it.
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from _fakes import FakeAPI, quiet_logging

from c64cast.sid import armsid
from c64cast.sid import sid_autoconfig as sa
from c64cast.sid.asid_sidmap import (
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_ADDR,
    ITEM_SOCKET2_EN,
    ITEM_SOCKET2_TYPE,
    plan_sid_map,
    plan_sid_map_for_addresses,
)
from c64cast.sid.sid_hw_config import (
    SidHwSession,
    apply_config,
    current_source_map,
    detect_socket_models,
)
from c64cast.sid.sid_resolved import SidHardwareState, describe_resolved_audio

CAT_ARMSID1 = armsid.CAT_ARMSID_FMT.format(n=1)


class _Chip:
    """One emulated SID of an ARMSID-family chip: the config-mode state machine
    of sid_device_armsid.cc, answering at registers 27-28."""

    def __init__(self, channel: str | None, model: str) -> None:
        self.channel = channel
        self.model = model
        self.regs = [0] * 32
        self.config_mode = False
        self.reply = b"\x00\x00"

    def write(self, reg: int, value: int) -> None:
        self.regs[reg] = value
        if reg == 29 and value == 0:
            self.config_mode = False
            return
        if (
            reg == 31
            and self.regs[29] == ord("S")
            and self.regs[30] == ord("I")
            and value == ord("D")
        ):
            self.config_mode = True
            self.reply = b"NO"
            return
        if not self.config_mode:
            return
        if reg == 31 and self.regs[29] == ord("S") and self.regs[30] == ord("E"):
            self.model = {ord("6"): "6581", ord("8"): "8580"}[value]
        if reg == 30 and value == ord("I"):
            command = chr(self.regs[31])
            if command == "I":
                self.reply = bytes([0x02, ord(self.channel)]) if self.channel else b"\x00\x00"
            elif command == "F":
                self.reply = self.model[:2].encode()
            elif command == "V":
                self.reply = b"\x03\x11"

    def read(self, reg: int) -> int:
        return self.reply[reg - 27] if reg in (27, 28) else 0


class ArmsidAPI(FakeAPI):
    """A FakeAPI with an ARMSID-family chip on the bus. Socket 1 answers at its
    configured address; an ARM2SID's right channel answers at socket 1's base
    plus the Ext DualSID split's offset, and nowhere while the split is off."""

    def __init__(self, *, kind: str = "ARM2SID", left: str = "8580", right: str = "8580"):
        super().__init__()
        ultimate = FakeAPI.ultimate()
        self.profile = ultimate.profile
        self.left = _Chip("L" if kind == "ARM2SID" else None, left)
        self.right = _Chip("R", right)
        self.has_right = kind == "ARM2SID"
        self.config_store = {
            CAT_SOCKETS: {
                ITEM_SOCKET1_EN: "Enabled",
                ITEM_SOCKET2_EN: "Enabled",
                # The firmware's own test reads the channel from the wrong
                # register and so reports an ARM2SID as ARMSID.
                ITEM_SOCKET1_TYPE: "ARMSID",
                ITEM_SOCKET2_TYPE: "None",
            },
            CAT_ADDRESSING: {
                ITEM_SOCKET1_ADDR: "$D400",
                ITEM_SOCKET2_ADDR: "$D420",
                armsid.ITEM_EXT_SPLIT: "A5",
            },
            CAT_ARMSID1: {armsid.ITEM_ARMSID_MODE: left},
        }

    def put_config_item(self, category, item, value, *, timeout=3.0):
        super().put_config_item(category, item, value, timeout=timeout)
        if (category, item) == (CAT_ARMSID1, armsid.ITEM_ARMSID_MODE):
            self.left.model = value

    def _chip_at(self, address: int) -> tuple[_Chip, int] | None:
        addressing = self.config_store[CAT_ADDRESSING]
        base1 = int(addressing[ITEM_SOCKET1_ADDR].lstrip("$"), 16)
        if base1 <= address < base1 + 0x20:
            return self.left, address - base1
        offset = armsid.split_offset(addressing.get(armsid.ITEM_EXT_SPLIT))
        if self.has_right and offset is not None and 0 <= address - base1 - offset < 0x20:
            return self.right, address - base1 - offset
        return None

    def write_memory(self, addr, data_hex):
        super().write_memory(addr, data_hex)
        hit = self._chip_at(int(addr, 16))
        if hit is not None:
            hit[0].write(hit[1], int(data_hex, 16))

    def read_memory(self, address, length, timeout=1.0):
        hit = self._chip_at(address)
        if hit is None:
            return bytes([0xFF] * length)
        chip, reg = hit
        return bytes(chip.read(reg + k) for k in range(length))


class QueuedArmsidAPI(ArmsidAPI):
    """An ArmsidAPI whose DMA writes reach the chip only at a flush, as on the
    U64 where they queue on the socket while reads and PUTs go over REST."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pending: list[tuple[str, str]] = []

    def write_memory(self, addr, data_hex):
        self.pending.append((addr, data_hex))

    def flush(self, timeout=5.0):
        pending, self.pending = self.pending, []
        for addr, data_hex in pending:
            super().write_memory(addr, data_hex)


class _NoSettle(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(armsid, "_SETTLE_S", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)


class ProtocolTest(_NoSettle):
    def test_probe_reads_kind_channel_and_model(self):
        api = ArmsidAPI(kind="ARM2SID", left="6581")
        reply = armsid.probe(api, 0xD400)
        self.assertEqual(reply, armsid.ArmsidReply(channel="L", model="6581"))
        assert reply is not None
        self.assertEqual(reply.kind, "ARM2SID")
        self.assertFalse(api.left.config_mode, "probe must leave config mode")

    def test_probe_of_a_plain_armsid_has_no_channel(self):
        reply = armsid.probe(ArmsidAPI(kind="ARMSID"), 0xD400)
        self.assertEqual(reply, armsid.ArmsidReply(channel=None, model="8580"))
        assert reply is not None
        self.assertEqual(reply.kind, "ARMSID")

    def test_probe_of_an_address_nothing_answers_is_none(self):
        self.assertIsNone(armsid.probe(ArmsidAPI(), 0xD500 + 0x40))

    def test_write_model_switches_the_chip(self):
        api = ArmsidAPI()
        armsid.write_model(api, 0xD420, "6581")
        self.assertEqual(api.right.model, "6581")
        self.assertEqual(api.left.model, "8580")
        self.assertFalse(api.right.config_mode)


class LabelTest(unittest.TestCase):
    def test_serves(self):
        self.assertTrue(armsid.socket_serves("ARMSID 8580", "6581"))
        self.assertTrue(armsid.socket_serves("ARM2SID R 6581", "8580"))
        self.assertTrue(armsid.socket_serves("6581", "6581"))
        self.assertFalse(armsid.socket_serves("6581", "8580"))
        self.assertFalse(armsid.socket_serves(None, "8580"))
        self.assertTrue(armsid.socket_serves("6581", None))

    def test_model_and_change(self):
        self.assertEqual(armsid.label_model("ARM2SID R 6581"), "6581")
        self.assertEqual(armsid.label_model("8580"), "8580")
        self.assertEqual(armsid.label_model("ARMSID ?"), "ARMSID ?")
        self.assertTrue(armsid.needs_model_change("ARMSID 8580", "6581"))
        self.assertFalse(armsid.needs_model_change("ARMSID 6581", "6581"))
        self.assertFalse(armsid.needs_model_change("6581", "8580"))

    def test_the_firmwares_bare_label_is_a_fixed_chip(self):
        # What a socket keeps when its probe went unanswered: nothing is known
        # about the chip, so nothing may be switched on it.
        self.assertFalse(armsid.is_reconfigurable("ARMSID"))
        self.assertFalse(armsid.socket_serves("ARMSID", "6581"))
        self.assertFalse(armsid.needs_model_change("ARMSID", "6581"))

    def test_an_unreadable_model_is_a_fixed_chip(self):
        # A switch there would leave no model for teardown to put back.
        for unread in ("ARMSID ?", "ARM2SID R ?"):
            self.assertFalse(armsid.is_reconfigurable(unread))
            self.assertFalse(armsid.socket_serves(unread, "6581"))
            self.assertFalse(armsid.needs_model_change(unread, "6581"))

    def test_right_channel(self):
        self.assertTrue(armsid.is_right_channel("ARM2SID R 8580"))
        self.assertFalse(armsid.is_right_channel("ARM2SID 8580"))


class DetectTest(_NoSettle):
    def test_arm2sid_with_split_on_labels_both_channels(self):
        api = ArmsidAPI(left="8580", right="6581")
        self.assertEqual(detect_socket_models(api), ("ARM2SID 8580", "ARM2SID R 6581"))
        self.assertEqual(api.config_puts, [], "the split was already on; nothing to toggle")

    def test_split_off_is_turned_on_for_the_probe_and_put_back(self):
        api = ArmsidAPI()
        api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT] = "Off"
        self.assertEqual(detect_socket_models(api), ("ARM2SID 8580", "ARM2SID R 8580"))
        self.assertEqual(
            [value for _c, item, value in api.config_puts if item == armsid.ITEM_EXT_SPLIT],
            ["A5", "Off"],
        )

    def test_queued_writes_land_before_each_read_and_split_restore(self):
        api = QueuedArmsidAPI(left="8580", right="6581")
        api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT] = "Off"
        self.assertEqual(detect_socket_models(api), ("ARM2SID 8580", "ARM2SID R 6581"))
        self.assertEqual(api.pending, [])
        self.assertFalse(api.right.config_mode, "left config mode before the split went off")
        armsid.set_socket_model(api, "socket2", "8580")
        self.assertEqual(api.right.model, "8580")
        self.assertFalse(api.right.config_mode)

    def test_an_unreadable_split_is_never_moved(self):
        api = ArmsidAPI()
        del api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT]
        self.assertEqual(detect_socket_models(api), ("ARM2SID 8580", None))
        self.assertNotIn(armsid.ITEM_EXT_SPLIT, {item for _c, item, _v in api.config_puts})

    def test_plain_armsid_has_no_right_channel(self):
        api = ArmsidAPI(kind="ARMSID", left="6581")
        self.assertEqual(detect_socket_models(api), ("ARMSID 6581", None))

    def test_unanswered_probe_keeps_the_firmware_label(self):
        api = ArmsidAPI()
        api.read_memory = lambda *a, **k: None  # type: ignore[method-assign]
        with self.assertLogs("c64cast.sid.armsid", "INFO"):
            self.assertEqual(detect_socket_models(api), ("ARMSID", None))

    def test_real_chips_are_never_probed(self):
        api = FakeAPI.ultimate()
        api.config_store[CAT_SOCKETS] = {ITEM_SOCKET1_TYPE: "6581", ITEM_SOCKET2_TYPE: "None"}
        self.assertEqual(detect_socket_models(api), ("6581", None))
        self.assertEqual(api.memories, {})

    def test_no_refresh_answers_from_the_last_probe_without_touching_the_bus(self):
        api = ArmsidAPI()
        self.assertEqual(detect_socket_models(api, refresh=False), ("ARMSID", None), "no probe yet")
        self.assertEqual(api.memories, {})
        detect_socket_models(api)
        api.memories.clear()
        self.assertEqual(
            detect_socket_models(api, refresh=False), ("ARM2SID 8580", "ARM2SID R 8580")
        )
        self.assertEqual(api.memories, {})


class SourceMapTest(_NoSettle):
    def test_right_channel_maps_through_the_split_not_socket_2s_address(self):
        api = ArmsidAPI()
        api.config_store[CAT_ADDRESSING][ITEM_SOCKET2_ADDR] = "$D500"
        detect_socket_models(api)
        addr_map = current_source_map(api)
        self.assertEqual(addr_map.get(0xD420), "socket2")
        self.assertNotIn(0xD500, addr_map)

    def test_split_off_leaves_the_right_channel_unmapped(self):
        api = ArmsidAPI()
        detect_socket_models(api)
        api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT] = "Off"
        self.assertNotIn("socket2", current_source_map(api).values())


class SetModelTest(_NoSettle):
    def test_left_goes_through_the_firmware_config_item(self):
        api = ArmsidAPI()
        detect_socket_models(api)
        armsid.set_socket_model(api, "socket1", "6581")
        self.assertIn((CAT_ARMSID1, armsid.ITEM_ARMSID_MODE, "6581"), api.config_puts)
        self.assertEqual(api.left.model, "6581")
        self.assertEqual(armsid.cached_labels(api), ("ARM2SID 6581", "ARM2SID R 8580"))

    def test_right_goes_through_its_registers_reaching_it_through_the_split(self):
        api = ArmsidAPI()
        detect_socket_models(api)
        api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT] = "Off"
        api.config_puts.clear()
        armsid.set_socket_model(api, "socket2", "6581")
        self.assertEqual(api.right.model, "6581")
        self.assertEqual(api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT], "Off")
        self.assertNotIn(CAT_ARMSID1, {c for c, _i, _v in api.config_puts})

    def test_right_channel_is_left_alone_when_the_split_is_unreadable(self):
        api = ArmsidAPI()
        detect_socket_models(api)
        del api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT]
        api.config_puts.clear()
        armsid.set_socket_model(api, "socket2", "6581")
        self.assertEqual(api.config_puts, [])
        self.assertEqual(api.right.model, "8580")

    def test_a_socket_whose_last_probe_went_unanswered_is_still_set(self):
        # A restore planned from "ARMSID 8580" has to land after a later probe
        # cached the firmware's bare label over it.
        for bare in ("ARMSID", "ARMSID ?"):
            with self.subTest(label=bare):
                api = ArmsidAPI(kind="ARMSID", left="6581")
                armsid._remember(api, (bare, None))
                armsid.set_socket_model(api, "socket1", "8580")
                self.assertEqual(api.left.model, "8580")
                self.assertEqual(armsid.cached_labels(api), ("ARMSID 8580", None))

    def test_a_socket_with_no_armsid_is_left_alone(self):
        api = FakeAPI.ultimate()
        armsid.set_socket_model(api, "socket1", "6581")
        self.assertEqual(api.config_puts, [])


class PlannerTest(unittest.TestCase):
    def test_single_sid_switches_the_armsid_instead_of_routing_to_a_core(self):
        plan = sa.plan_sid_model_config(
            chips=((0xD400, "6581"),),
            current_addr_map={0xD400: "socket1"},
            socket_models=("ARMSID 8580", None),
            ultisid_allowed=True,
        )
        self.assertEqual(plan, {(armsid.CAT_SOCKET_MODEL, "socket1"): "6581"})

    def test_single_sid_already_in_the_right_mode_is_a_noop(self):
        with self.assertLogs("c64cast.sid.sid_autoconfig", "INFO"):
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "8580"),),
                current_addr_map={0xD400: "socket1"},
                socket_models=("ARMSID 8580", None),
                ultisid_allowed=True,
            )
        self.assertIsNone(plan)

    def test_the_right_channel_is_never_swapped_into_place(self):
        # socket1 is a fixed 6581; the right channel could serve 8580 but cannot
        # be moved to $D400, so the chip goes to a core.
        with self.assertLogs("c64cast.sid.sid_autoconfig", "INFO"):
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "8580"),),
                current_addr_map={0xD400: "socket1"},
                socket_models=("6581", "ARM2SID R 6581"),
                ultisid_allowed=True,
            )
        assert plan is not None
        self.assertNotIn((CAT_ADDRESSING, ITEM_SOCKET2_ADDR), plan)
        self.assertNotIn((armsid.CAT_SOCKET_MODEL, "socket2"), plan)

    def test_first_model_pass_maps_the_right_channel_by_the_split(self):
        # Socket 2's own items say Enabled at $D420, but with the split off the
        # right channel answers nowhere: the $D420 chip must not be left there.
        api = ArmsidAPI()
        api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT] = "Off"
        header = SimpleNamespace(sid_addresses=(0xD400, 0xD420), sid_models=("8580", "8580"))
        with mock.patch.object(armsid, "_SETTLE_S", 0.0), quiet_logging():
            plan = sa.plan_model_config_for_header(api, header, "auto")
        self.assertIn("$D420", (plan or {}).values())

    def test_a_core_displacing_the_right_channel_turns_the_split_off(self):
        # A right channel whose model reply was unknown cannot serve the chip,
        # so a core takes $D420; socket 2's enable would not silence it.
        with self.assertLogs("c64cast.sid.sid_autoconfig", "INFO"):
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "8580"), (0xD420, "6581")),
                current_addr_map={0xD400: "socket1", 0xD420: "socket2"},
                socket_models=("ARM2SID 8580", "ARM2SID R ?"),
                ultisid_allowed=True,
            )
        assert plan is not None
        self.assertEqual(plan[(CAT_ADDRESSING, armsid.ITEM_EXT_SPLIT)], armsid.EXT_SPLIT_OFF)
        self.assertNotIn((CAT_SOCKETS, ITEM_SOCKET2_EN), plan)

    def test_socket1_is_not_displaced_under_a_playing_right_channel(self):
        # The right channel is decoded through socket 1's enable: disabling
        # socket 1 for a core would silence the $D420 chip it plays.
        with self.assertLogs("c64cast.sid.sid_autoconfig", "INFO") as logs:
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "6581"), (0xD420, "8580")),
                current_addr_map={0xD400: "socket1", 0xD420: "socket2"},
                socket_models=("ARM2SID ?", "ARM2SID R 8580"),
                ultisid_allowed=True,
            )
        self.assertIsNone(plan)
        self.assertTrue(any("cannot be displaced" in line for line in logs.output))

    def test_socket1_is_not_swapped_away_under_a_playing_right_channel(self):
        # The right channel answers at socket 1's base + $20: swapping socket 1
        # to $D500 would carry the right channel off the $D420 chip it plays.
        with self.assertLogs("c64cast.sid.sid_autoconfig", "INFO"):
            plan = sa.plan_sid_model_config(
                chips=((0xD400, None), (0xD420, "8580"), (0xD500, "6581")),
                current_addr_map={0xD400: "socket1", 0xD420: "socket2"},
                socket_models=("ARM2SID 8580", "ARM2SID R 6581"),
                ultisid_allowed=True,
            )
        assert plan is not None
        self.assertNotIn((CAT_ADDRESSING, ITEM_SOCKET1_ADDR), plan)
        self.assertNotIn((CAT_SOCKETS, ITEM_SOCKET1_EN), plan)
        self.assertNotIn((armsid.CAT_SOCKET_MODEL, "socket1"), plan)

    def test_socket1_is_displaced_when_the_right_channel_plays_nothing(self):
        with self.assertLogs("c64cast.sid.sid_autoconfig", "INFO"):
            plan = sa.plan_sid_model_config(
                chips=((0xD400, "6581"),),
                current_addr_map={0xD400: "socket1", 0xD420: "socket2"},
                socket_models=("ARM2SID ?", "ARM2SID R 8580"),
                ultisid_allowed=True,
            )
        assert plan is not None
        self.assertEqual(plan[(CAT_SOCKETS, ITEM_SOCKET1_EN)], "Disabled")

    def test_two_sid_tune_lands_on_both_channels_with_their_models(self):
        sm = plan_sid_map_for_addresses(
            (0xD400, 0xD420),
            socket_models=("ARM2SID 8580", "ARM2SID R 8580"),
            required_models=("6581", "8580"),
        )
        assert sm is not None
        self.assertEqual(sm.sources, ("socket1", "socket2"))
        self.assertEqual(sm.config[(CAT_ADDRESSING, armsid.ITEM_EXT_SPLIT)], "A5")
        self.assertEqual(sm.config[(armsid.CAT_SOCKET_MODEL, "socket1")], "6581")
        self.assertNotIn((armsid.CAT_SOCKET_MODEL, "socket2"), sm.config)
        self.assertNotIn(
            (CAT_SOCKETS, ITEM_SOCKET2_EN), {k for k, v in sm.config.items() if v == "Enabled"}
        )

    def test_unclaimed_right_channel_turns_the_split_off(self):
        sm = plan_sid_map_for_addresses(
            (0xD400, 0xD500),
            socket_models=("ARM2SID 8580", "ARM2SID R 8580"),
        )
        assert sm is not None
        self.assertEqual(sm.config[(CAT_ADDRESSING, armsid.ITEM_EXT_SPLIT)], "Off")
        self.assertEqual(sm.sources[0], "socket1")
        self.assertTrue(sm.sources[1].startswith("ultisid"))

    def test_asid_layout_uses_the_right_channel(self):
        sm = plan_sid_map(
            2,
            socket1_present=True,
            socket2_present=True,
            socket_models=("ARM2SID 8580", "ARM2SID R 8580"),
        )
        self.assertEqual(sm.sources, ("socket1", "socket2"))
        self.assertEqual(sm.config[(CAT_ADDRESSING, armsid.ITEM_EXT_SPLIT)], "A5")


class SessionRoundTripTest(_NoSettle):
    def test_model_changes_are_restored_with_the_rest(self):
        api = ArmsidAPI(left="8580", right="8580")
        api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT] = "Off"
        models = detect_socket_models(api)
        sm = plan_sid_map_for_addresses(
            (0xD400, 0xD420), socket_models=models, required_models=("6581", "6581")
        )
        assert sm is not None
        session = SidHwSession(api)
        session.snapshot()
        apply_config(api, sm.config)
        self.assertEqual((api.left.model, api.right.model), ("6581", "6581"))
        self.assertEqual(api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT], "A5")
        session.restore()
        self.assertEqual((api.left.model, api.right.model), ("8580", "8580"))
        self.assertEqual(api.config_store[CAT_ADDRESSING][armsid.ITEM_EXT_SPLIT], "Off")

    def test_right_channel_follows_socket_1_when_the_plan_moves_it(self):
        api = ArmsidAPI(left="8580", right="8580")
        api.config_store[CAT_ADDRESSING][ITEM_SOCKET1_ADDR] = "$D500"
        models = detect_socket_models(api)
        sm = plan_sid_map_for_addresses(
            (0xD400, 0xD420), socket_models=models, required_models=("6581", "6581")
        )
        assert sm is not None
        session = SidHwSession(api)
        session.snapshot()
        apply_config(api, sm.config)
        self.assertEqual((api.left.model, api.right.model), ("6581", "6581"))
        session.restore()
        self.assertEqual((api.left.model, api.right.model), ("8580", "8580"))
        self.assertEqual(api.config_store[CAT_ADDRESSING][ITEM_SOCKET1_ADDR], "$D500")


class ResolvedLineTest(unittest.TestCase):
    def test_armsid_label_is_judged_by_its_model(self):
        state = SidHardwareState(
            addr_map={0xD400: "socket1", 0xD420: "socket2"},
            socket_models=("ARM2SID 6581", "ARM2SID R 8580"),
            ultisid_curves={},
            mixer={"Vol Socket 1": " 0 dB", "Vol Socket 2": " 0 dB"},
        )
        resolved = describe_resolved_audio(state, (0xD400, 0xD420), ("6581", "8580"))
        self.assertTrue(resolved.clean, resolved.summary)
        self.assertIn("socket2 (ARM2SID R 8580)", resolved.summary)
        wrong = describe_resolved_audio(state, (0xD400,), ("8580",))
        self.assertFalse(wrong.clean)


if __name__ == "__main__":
    unittest.main()
