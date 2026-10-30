"""Direct contract tests for c64cast._midi.open_input_port — the shared
port resolver behind MidiScene, AsidScene and MidiControlListener's own
`_open_port`. Exercised with `mido` mocked out, so no real MIDI hardware (or
even the `midi` extra) is needed."""

from __future__ import annotations

import logging
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FrozenClock  # noqa: E402

from c64cast import _midi  # noqa: E402


class _FakePort:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_mido(names: list[str], opened: list[str]):
    fake = mock.MagicMock()
    fake.get_input_names.return_value = names
    fake.open_input.side_effect = lambda n: opened.append(n) or _FakePort()
    return mock.patch.object(_midi, "mido", fake)


class OpenInputPortTest(unittest.TestCase):
    def test_default_spec_opens_the_first_port(self):
        opened: list[str] = []
        with _patch_mido(["Port A", "Port B"], opened):
            port, name = _midi.open_input_port(None, label="test")
        self.assertEqual(name, "Port A")
        self.assertEqual(opened, ["Port A"])

    def test_empty_and_default_string_also_pick_the_first_port(self):
        for spec in ("", "default"):
            with self.subTest(spec=spec):
                opened: list[str] = []
                with _patch_mido(["Port A"], opened):
                    _midi.open_input_port(spec, label="test")
                self.assertEqual(opened, ["Port A"])

    def test_no_ports_available_raises_with_the_caller_label(self):
        with _patch_mido([], []):
            with self.assertRaisesRegex(RuntimeError, "test: no MIDI input ports"):
                _midi.open_input_port(None, label="test")

    def test_substring_match_is_case_insensitive(self):
        opened: list[str] = []
        with _patch_mido(["IAC Bus 1", "Launch Control XL"], opened):
            _midi.open_input_port("launch", label="test")
        self.assertEqual(opened, ["Launch Control XL"])

    def test_no_match_raises_naming_the_spec_and_available_ports(self):
        with _patch_mido(["IAC Bus 1"], []):
            with self.assertRaisesRegex(RuntimeError, "nonexistent.*IAC Bus 1"):
                _midi.open_input_port("nonexistent", label="test")

    def test_missing_midi_extra_raises_a_named_runtime_error_not_an_assert(self):
        # Regression: a bare `assert mido is not None` is stripped by `python -O`
        # and otherwise surfaces as an AttributeError naming no missing extra.
        with mock.patch.object(_midi, "MIDI_AVAILABLE", False):
            with self.assertRaisesRegex(RuntimeError, "midi.*extra"):
                _midi.open_input_port(None, label="test")


class PollPendingTest(unittest.TestCase):
    """The bounded, stop-aware drain both SID scene readers use in place of
    mido's `iter_pending()`. The bound is what keeps a message flood from
    starving the register flush that follows the drain, and the `stop` re-check
    is what keeps a flooded reader from outliving a bounded teardown join."""

    def setUp(self):
        # These pin the *count* bound, so freeze the clock the *work* bound reads:
        # a stalled worker must not release a pass early and flake the count.
        patcher = mock.patch.object(_midi, "time", FrozenClock(0.0, "monotonic"))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _port(n_messages, stop=None, stop_after=None):
        """A port with `n_messages` queued, then empty. `stop_after` sets
        `stop` once that many messages have been handed out."""
        state = {"served": 0}

        def poll():
            if state["served"] >= n_messages:
                return None
            state["served"] += 1
            if stop is not None and state["served"] == stop_after:
                stop.set()
            return f"msg{state['served']}"

        return SimpleNamespace(poll=poll, served=state)

    def test_drains_everything_below_the_bound(self):
        stop = threading.Event()
        port = self._port(5)
        self.assertEqual(len(list(_midi.poll_pending(port, stop))), 5)

    def test_never_hands_out_more_than_the_bound_in_one_pass(self):
        stop = threading.Event()
        port = self._port(_midi.MAX_MSGS_PER_DRAIN * 10)
        drained = list(_midi.poll_pending(port, stop))
        self.assertEqual(len(drained), _midi.MAX_MSGS_PER_DRAIN)
        # And the rest is still there for the caller's next pass.
        self.assertEqual(len(list(_midi.poll_pending(port, stop))), _midi.MAX_MSGS_PER_DRAIN)

    def test_stops_mid_pass_once_the_stop_event_is_set(self):
        stop = threading.Event()
        port = self._port(_midi.MAX_MSGS_PER_DRAIN * 10, stop=stop, stop_after=3)
        self.assertEqual(len(list(_midi.poll_pending(port, stop))), 2)

        # Three polled, two handed out: the `stop` re-check sits *after* the poll,
        # so the message that tripped it is off the port's queue and never reaches
        # a consumer. That asymmetry is the one release that drops — the budget
        # check is before the poll so it cannot.
        self.assertEqual(port.served["served"], 3)

    def test_a_pass_entered_with_stop_already_set_drops_the_message_it_polls(self):
        # The limit case of the above: nothing is yielded, but the pass is not a
        # no-op — it polls once and discards, so a caller cannot treat a stopped
        # pass as having left the queue untouched.
        stop = threading.Event()
        stop.set()
        port = self._port(3)
        self.assertEqual(list(_midi.poll_pending(port, stop)), [])
        self.assertEqual(port.served["served"], 1)

    def test_an_explicit_limit_overrides_the_default(self):
        stop = threading.Event()
        port = self._port(50)
        self.assertEqual(len(list(_midi.poll_pending(port, stop, limit=4))), 4)

    def test_the_count_bound_is_read_when_the_pass_runs_not_when_the_file_loads(self):
        # As a parameter default the constant was bound at definition time, so
        # rebinding it here was a silent no-op — while `_midi.time`, this file's
        # other injection point, is rebound exactly this way and does work.
        stop = threading.Event()
        port = self._port(50)
        with mock.patch.object(_midi, "MAX_MSGS_PER_DRAIN", 3):
            self.assertEqual(len(list(_midi.poll_pending(port, stop))), 3)

    def test_a_zero_limit_hands_out_nothing(self):
        # The bound is restored from `None`, not from falsiness: `limit or
        # MAX_MSGS_PER_DRAIN` would read a deliberate zero as "use the
        # default" and hand out 64 messages to a caller that asked for none.
        stop = threading.Event()
        port = self._port(50)
        self.assertEqual(list(_midi.poll_pending(port, stop, limit=0)), [])
        # And the port was never polled. That is the load-bearing half: a poll
        # takes the message off the port's queue, so a count check on the far side
        # of one would eat a message while still yielding nothing here. Scoped to
        # the budget: the `stop` release does drop, and has its own two tests.
        self.assertEqual(port.served["served"], 0)


class DrainWorkBoundTest(unittest.TestCase):
    """The count bound alone is not enough: it bounds *messages* while the wire
    chooses the *work* per message. One WARNING through the default terminal
    handler costs ~322 us, so 64 of them is 20.6 ms inside a pass whose loop is
    otherwise sub-millisecond — long enough to starve the coalesced flush and
    the stop check the count bound exists to reach."""

    @staticmethod
    def _port(n_messages):
        state = {"served": 0}

        def poll():
            if state["served"] >= n_messages:
                return None
            state["served"] += 1
            return f"msg{state['served']}"

        return SimpleNamespace(poll=poll, served=state)

    def test_a_pass_releases_once_it_has_spent_its_work_budget(self):
        stop = threading.Event()
        port = self._port(1000)
        expensive = FrozenClock(1000.0, "monotonic", _midi.MAX_DRAIN_WORK_S / 3)
        with mock.patch.object(_midi, "time", expensive):
            drained = list(_midi.poll_pending(port, stop))
        self.assertLess(len(drained), _midi.MAX_MSGS_PER_DRAIN)
        self.assertGreater(len(drained), 0)

    def test_a_pass_always_hands_out_at_least_one_message(self):
        # A consumer slower than the entire budget must still make progress
        # rather than spin: the deadline is not checked before the first message.
        stop = threading.Event()
        port = self._port(1000)
        with mock.patch.object(_midi, "time", FrozenClock(1000.0, "monotonic", 1.0)):
            self.assertEqual(len(list(_midi.poll_pending(port, stop, budget_s=0.0))), 1)

    def test_a_zero_work_budget_releases_after_one_message(self):
        # The bound is restored from `None`, not from falsiness. The sibling test
        # above cannot show this: its clock steps a whole second, so a zero budget
        # and the real default both release after one message.
        stop = threading.Event()
        port = self._port(1000)
        with mock.patch.object(_midi, "time", FrozenClock(1000.0, "monotonic")):
            self.assertEqual(len(list(_midi.poll_pending(port, stop, budget_s=0.0))), 1)

    def test_the_work_budget_is_read_when_the_pass_runs_not_when_the_file_loads(self):
        # Widened rather than shrunk, so the assertion cannot pass on the bound the
        # sibling test already produces: only a budget read at call time lets the
        # count bound be the one that binds.
        stop = threading.Event()
        port = self._port(1000)
        # Sized off the real constant before the patch is entered: a parenthesized
        # `with` evaluates each expression after entering the one before it, so
        # reading it inside would size the clock off 1000.0 and release on three.
        step = _midi.MAX_DRAIN_WORK_S / 3
        with (
            mock.patch.object(_midi, "MAX_DRAIN_WORK_S", 1000.0),
            mock.patch.object(_midi, "time", FrozenClock(1000.0, "monotonic", step)),
        ):
            drained = list(_midi.poll_pending(port, stop))
        self.assertEqual(len(drained), _midi.MAX_MSGS_PER_DRAIN)

    def test_releasing_the_pass_on_the_budget_drops_no_message(self):
        # The deadline is checked *before* `port.poll()`, never after — a poll
        # has already taken the message off the port's queue, so a check on the
        # far side of it would silently eat a frame of SID register writes.
        stop = threading.Event()
        port = self._port(20)
        seen = []
        clock = FrozenClock(1000.0, "monotonic", _midi.MAX_DRAIN_WORK_S / 3)
        with mock.patch.object(_midi, "time", clock):
            for _ in range(20):
                seen.extend(_midi.poll_pending(port, stop))
        self.assertEqual(seen, [f"msg{i}" for i in range(1, 21)])

    def test_the_default_work_budget_is_a_fraction_of_the_flush_period(self):
        # The relationship, against the two scene constants that define the period
        # rather than a literal copy: `MAX_DRAIN_WORK_S` exists to keep a pass from
        # eating the flush that follows it. This holds for the *default*, which is
        # AsidScene's budget; MidiScene passes its own — the sibling test below.
        from c64cast.sid import asid_scene, midi_scene

        protected_s = min(asid_scene._FLUSH_INTERVAL_S, midi_scene._CONTROL_FLUSH_INTERVAL_S)
        self.assertGreater(_midi.MAX_DRAIN_WORK_S, 0.0)
        self.assertLess(_midi.MAX_DRAIN_WORK_S, protected_s)

    def test_midi_scenes_own_budget_overruns_that_fraction_on_a_slow_link(self):
        # The invariant above is the default's, not the system's. MidiScene's
        # reader passes `_drain_budget_s`, which widens until a worst-case chord
        # retires in one pass: on an Ultimate 31.332 ms against a 16.667 ms flush
        # period, 1.88x the fraction the default is held to.
        #
        # A deliberate latency trade, not a missing bound: the chord's notes land
        # together and the wheel/CC flush after that pass is late by the
        # difference. Pinned with the ratio so that retuning the flush period, the
        # write cost model, `_WRITES_PER_NOTE` or `_NOTES_PER_DRAIN` comes past it.
        #
        # Against `_CONTROL_FLUSH_INTERVAL_S` alone, not the `min()` the sibling
        # test takes: a `min()` over both scenes' intervals leaves the larger one
        # unpinned.
        from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE
        from c64cast.sid import midi_scene

        protected_s = midi_scene._CONTROL_FLUSH_INTERVAL_S
        ultimate_s = midi_scene._drain_budget_s(ULTIMATE_PROFILE)
        self.assertAlmostEqual(ultimate_s / protected_s, 1.88, places=2)

        # A link whose writes are cheap keeps the default, and so keeps the
        # invariant: the overrun is the slow link's, not every link's.
        self.assertEqual(midi_scene._drain_budget_s(TEENSYROM_PROFILE), _midi.MAX_DRAIN_WORK_S)

    def test_the_shared_default_is_midi_scenes_floor_not_its_budget(self):
        # config.md names `midi_scene.MAX_DRAIN_WORK_S` as the lever, once a reader
        # has learned that rebinding `_midi`'s own copy is inert for MidiScene. It
        # is a `max()` operand, so it only moves the answer from above the
        # profile-derived term (31.332 ms on an Ultimate) — rebinding it *down* is
        # a second silent no-op. Which operand wins is the fact worth pinning.
        from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE
        from c64cast.sid import midi_scene

        ultimate_s = midi_scene._drain_budget_s(ULTIMATE_PROFILE)
        with mock.patch.object(midi_scene, "MAX_DRAIN_WORK_S", 0.0):
            self.assertEqual(midi_scene._drain_budget_s(ULTIMATE_PROFILE), ultimate_s)
            # The cheap link is where the constant is the operative term, so the
            # same rebind moves it — down to that link's derived floor, not to zero.
            teensy_s = midi_scene._drain_budget_s(TEENSYROM_PROFILE)
        self.assertLess(teensy_s, _midi.MAX_DRAIN_WORK_S)
        self.assertGreater(teensy_s, 0.0)

        # From above, both move: the operand that wins is decided per call, not
        # per profile, so a wide enough rebind is the lever the doc claimed.
        with mock.patch.object(midi_scene, "MAX_DRAIN_WORK_S", 1.0):
            self.assertEqual(midi_scene._drain_budget_s(ULTIMATE_PROFILE), 1.0)
            self.assertEqual(midi_scene._drain_budget_s(TEENSYROM_PROFILE), 1.0)


class PortWatchReportTest(unittest.TestCase):
    """On macOS a replugged USB controller keeps feeding the port opened before the
    unplug, so the loss warning no longer sends anyone to restart c64cast."""

    PORT = "Pads MIDI 1"

    def setUp(self):
        self.lost: list[bool] = []
        self.watch = _midi.PortWatch(
            lambda: self.PORT,
            lambda: self.lost.append(True),
            label="test",
            releases="every held pad",
            logger=logging.getLogger("c64cast._midi"),
            name="test-port-watch",
        )

    def _check(self, names: list[str]) -> None:
        with mock.patch.object(_midi, "mido") as fake:
            fake.get_input_names.return_value = names
            self.watch.check()

    def test_the_loss_names_what_it_releases_and_nothing_to_restart(self):
        with self.assertLogs("c64cast._midi", level="WARNING") as logs:
            self._check([])
        self.assertEqual(self.lost, [True])
        self.assertIn("releasing every held pad", logs.output[0])
        self.assertNotIn("restart", logs.output[0])

    def test_a_returning_port_is_counted_without_a_second_release(self):
        with self.assertLogs("c64cast._midi", level="WARNING"):
            self._check([])
        self._check([self.PORT])
        self._check([self.PORT])
        self.assertEqual(self.lost, [True])
        self.assertEqual(self.watch.returns, 1)


class PortReopenerTest(unittest.TestCase):
    """A replugged controller must not need a restart: a re-created virtual
    source, and off macOS any device, comes back as a new endpoint the port
    opened before the unplug never hears."""

    PORT = "Pads MIDI 1"

    def setUp(self):
        self.names: list[str] = [self.PORT]
        self.opened: list[tuple[str, str]] = []
        self.fresh: list[_FakePort] = []
        self.failures: list[Exception] = []
        self.watch = _midi.PortWatch(
            lambda: self.PORT,
            lambda: None,
            label="test",
            releases="every held pad",
            logger=logging.getLogger("c64cast._midi"),
            name="test-port-watch",
        )

    def _open(self, kind: str, name: str) -> _FakePort:
        self.opened.append((kind, name))
        if self.failures:
            raise self.failures.pop(0)
        port = _FakePort()
        self.fresh.append(port)
        return port

    def _fake_mido(self):
        fake = mock.MagicMock()
        fake.get_input_names.side_effect = lambda: list(self.names)
        fake.open_input.side_effect = lambda n: self._open("input", n)
        fake.open_output.side_effect = lambda n: self._open("output", n)
        return mock.patch.object(_midi, "mido", fake)

    def _unplug_and_replug(self) -> None:
        with self._fake_mido(), self.assertLogs("c64cast._midi", level="WARNING"):
            self.names[:] = []
            self.watch.check()
        with self._fake_mido():
            self.names[:] = [self.PORT]
            self.watch.check()

    def _call(self, reopen, port):
        with self._fake_mido():
            return reopen(port)

    def test_a_port_that_never_left_is_handed_back_untouched(self):
        reopen = self.watch.reopener()
        old = _FakePort()
        with self._fake_mido():
            self.watch.check()
        self.assertIs(self._call(reopen, old), old)
        self.assertFalse(old.closed)
        self.assertEqual(self.opened, [])

    def test_a_returned_port_is_closed_and_opened_again_by_its_name(self):
        reopen = self.watch.reopener()
        old = _FakePort()
        self._unplug_and_replug()
        with self.assertLogs("c64cast._midi", level="INFO") as logs:
            fresh = self._call(reopen, old)
        self.assertTrue(old.closed)
        self.assertEqual(self.opened, [("input", self.PORT)])
        self.assertIs(fresh, self.fresh[0])
        self.assertIn(f"{self.PORT!r} is back; reconnected", logs.output[0])
        self.assertNotIn("restart", logs.output[0])
        self.assertIs(self._call(reopen, fresh), fresh)
        self.assertEqual(len(self.opened), 1)

    def test_an_output_reopens_as_an_output(self):
        reopen = self.watch.reopener(output="Pads Out")
        self._unplug_and_replug()
        with self.assertLogs("c64cast._midi", level="INFO"):
            self._call(reopen, _FakePort())
        self.assertEqual(self.opened, [("output", "Pads Out")])

    def test_a_failed_reopen_warns_and_the_next_return_retries(self):
        reopen = self.watch.reopener()
        old = _FakePort()
        self.failures.append(OSError("port vanished again"))
        self._unplug_and_replug()
        with self.assertLogs("c64cast._midi", level="WARNING") as logs:
            self.assertIsNone(self._call(reopen, old))
        self.assertTrue(old.closed)
        self.assertIn("plug it in again to retry", logs.output[0])
        self.assertIsNone(self._call(reopen, None))

        self._unplug_and_replug()
        with self.assertLogs("c64cast._midi", level="INFO"):
            fresh = self._call(reopen, None)
        self.assertIs(fresh, self.fresh[0])
        self.assertEqual(self.opened, [("input", self.PORT)] * 2)

    def test_two_returns_before_a_call_reopen_once(self):
        reopen = self.watch.reopener()
        self._unplug_and_replug()
        self._unplug_and_replug()
        with self.assertLogs("c64cast._midi", level="INFO"):
            self._call(reopen, _FakePort())
        self.assertEqual(len(self.opened), 1)


class PortListOnMacOSTest(unittest.TestCase):
    """macOS refreshes the port list only on the run loop of the thread that
    made the process's first MIDI client, so `PortWatch`'s thread saw an
    unplugged controller stay listed. The first CoreMIDI call through
    `_midi.mido` now hands that first client to a thread that services its run
    loop."""

    def setUp(self):
        self.listings: list[str] = []
        self.fake_mido = mock.MagicMock()
        self.fake_mido.get_input_names.side_effect = self._list

        self.slices: list[bool] = []
        for patcher in (
            mock.patch.object(_midi, "_mido", self.fake_mido),
            mock.patch.object(_midi, "_port_list_thread", None),
            mock.patch.object(_midi, "_run_loop_slice", lambda: self.slices.pop(0)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _list(self) -> list[str]:
        name = threading.current_thread().name
        if name == "midi-port-list":
            # Slow enough that a caller not waiting for this listing overtakes it.
            time.sleep(0.05)
        self.listings.append(name)
        return ["Pads"]

    def _list_ports_until_the_run_loop_empties(self, calls: int, slices: list[bool]) -> None:
        self.slices[:] = slices
        with self.assertLogs("c64cast._midi", level="WARNING") as logs:
            wrapped = _midi._mido_for("darwin")
            for _ in range(calls):
                wrapped.get_input_names()
            assert _midi._port_list_thread is not None
            _midi._port_list_thread.join(5.0)
        self.assertIn("will not be noticed", logs.output[0])

    def test_the_port_list_thread_makes_the_first_listing(self):
        self._list_ports_until_the_run_loop_empties(1, [False])
        self.assertEqual(self.listings, ["midi-port-list", threading.current_thread().name])

    def test_only_the_first_call_starts_the_thread(self):
        self._list_ports_until_the_run_loop_empties(3, [False])
        self.assertEqual(self.listings.count("midi-port-list"), 1)

    def test_the_thread_services_its_run_loop_until_it_has_nothing_to_serve(self):
        self._list_ports_until_the_run_loop_empties(1, [True, True, False])
        self.assertEqual(self.slices, [])

    def test_a_first_listing_that_raises_does_not_hang_the_caller(self):
        escaped: list[BaseException | None] = []
        self.fake_mido.get_input_names.side_effect = OSError("no MIDI client")
        with mock.patch.object(
            threading, "excepthook", lambda args: escaped.append(args.exc_value)
        ):
            caller = threading.Thread(
                target=_midi._mido_for("darwin").get_input_names, name="caller", daemon=True
            )
            caller.start()
            caller.join(5.0)
            self.assertFalse(caller.is_alive())
            assert _midi._port_list_thread is not None
            _midi._port_list_thread.join(5.0)
        self.assertEqual([type(e) for e in escaped], [OSError, OSError])

    def test_building_a_message_starts_no_thread(self):
        _ = _midi._mido_for("darwin").Message
        self.assertIsNone(_midi._port_list_thread)

    def test_other_platforms_get_mido_itself(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                self.assertIs(_midi._mido_for(platform), self.fake_mido)


class RunLoopSliceTest(unittest.TestCase):
    def _slice_answering(self, result: int) -> tuple[bool, list[tuple]]:
        calls: list[tuple] = []

        def run_in_mode(*args):
            calls.append(args)
            return result

        with mock.patch.object(_midi, "_corefoundation", lambda: (run_in_mode, "default-mode")):
            return _midi._run_loop_slice(), calls

    def test_a_run_loop_with_no_sources_ends_the_service(self):
        serving, _calls = self._slice_answering(1)  # kCFRunLoopRunFinished
        self.assertFalse(serving)

    def test_a_timed_out_slice_keeps_serving(self):
        serving, calls = self._slice_answering(3)  # kCFRunLoopRunTimedOut
        self.assertTrue(serving)
        self.assertEqual(calls, [("default-mode", _midi._RUN_LOOP_SLICE_S, False)])


if __name__ == "__main__":
    unittest.main()
