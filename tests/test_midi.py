"""Direct contract tests for c64cast._midi.open_input_port — the shared
port resolver behind MidiScene, AsidScene and MidiControlListener's own
`_open_port`. Exercised with `mido` mocked out, so no real MIDI hardware (or
even the `midi` extra) is needed."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from c64cast import _midi


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
        # Regression: this used to be a bare `assert mido is not None`, which
        # `python -O` strips and which otherwise surfaces as an unhelpful
        # AttributeError on None, naming nothing about the missing extra.
        with mock.patch.object(_midi, "MIDI_AVAILABLE", False):
            with self.assertRaisesRegex(RuntimeError, "midi.*extra"):
                _midi.open_input_port(None, label="test")


class PollPendingTest(unittest.TestCase):
    """The bounded, stop-aware drain both SID scene readers use in place of
    mido's `iter_pending()`. The bound is what keeps a message flood from
    starving the register flush that follows the drain, and the `stop` re-check
    is what keeps a flooded reader from outliving a bounded teardown join."""

    def setUp(self):
        # These pin the *count* bound, so freeze the clock the *work* bound
        # reads: a stalled worker on a loaded machine must not be able to
        # release a pass early and turn a count assertion into a flake.
        patcher = mock.patch.object(_midi, "_monotonic", lambda: 0.0)
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

        # Three polled, two handed out: the `stop` re-check sits *after* the
        # poll, so the message that tripped it is off the port's queue and
        # never reaches a consumer. That asymmetry is the one release that
        # drops — the budget check is deliberately before the poll so it
        # cannot — and asserting only the yield count left it unpinned while
        # two paragraphs of the docstring said a release drops nothing.
        self.assertEqual(port.served["served"], 3)

    def test_a_pass_entered_with_stop_already_set_drops_the_message_it_polls(self):
        # The limit case of the above, and the one the zero-bound paragraph
        # describes: nothing is yielded, but the pass is not a no-op — it polls
        # once and discards. A caller cannot treat a stopped pass as having left
        # the queue untouched.
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
        # rebinding it here was a silent no-op — and this file's other
        # injection point, `_monotonic`, is rebound exactly this way and does
        # work. One idiom that does nothing beside one that does is how a test
        # gets written, passes, and pins nothing.
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
        # takes the message off the port's queue, so a count check moved to the
        # far side of one would eat a message while still yielding nothing
        # here — the same "releasing on the budget drops no message" invariant
        # the deadline path has its own test for. Scoped to the budget on
        # purpose: the `stop` release does drop, and has its own two tests.
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

    @staticmethod
    def _clock(step):
        """A monotonic stand-in advancing `step` per read, so a pass can burn
        its work budget without the test spending that time."""
        state = {"now": 1000.0}

        def monotonic():
            state["now"] += step
            return state["now"]

        return monotonic

    def test_a_pass_releases_once_it_has_spent_its_work_budget(self):
        stop = threading.Event()
        port = self._port(1000)
        expensive = self._clock(_midi.MAX_DRAIN_WORK_S / 3)
        with mock.patch.object(_midi, "_monotonic", expensive):
            drained = list(_midi.poll_pending(port, stop))
        self.assertLess(len(drained), _midi.MAX_MSGS_PER_DRAIN)
        self.assertGreater(len(drained), 0)

    def test_a_pass_always_hands_out_at_least_one_message(self):
        # A consumer slower than the entire budget must still make progress
        # rather than spin: the deadline is not checked before the first message.
        stop = threading.Event()
        port = self._port(1000)
        with mock.patch.object(_midi, "_monotonic", self._clock(1.0)):
            self.assertEqual(len(list(_midi.poll_pending(port, stop, budget_s=0.0))), 1)

    def test_a_zero_work_budget_releases_after_one_message(self):
        # The bound is restored from `None`, not from falsiness. The sibling
        # test above cannot show this: its clock steps a whole second, so a
        # zero budget and the real default both release after one message. A
        # clock that does not advance separates them — a real budget then never
        # expires and the pass runs to the count bound.
        stop = threading.Event()
        port = self._port(1000)
        with mock.patch.object(_midi, "_monotonic", lambda: 1000.0):
            self.assertEqual(len(list(_midi.poll_pending(port, stop, budget_s=0.0))), 1)

    def test_the_work_budget_is_read_when_the_pass_runs_not_when_the_file_loads(self):
        # Widened rather than shrunk, so the assertion cannot pass on the
        # bound the sibling test above already produces: with the real default
        # this clock releases the pass after a handful of messages, and only a
        # budget read at call time lets the count bound be the one that binds.
        stop = threading.Event()
        port = self._port(1000)
        # Sized off the real constant before the patch is entered: a
        # parenthesized `with` evaluates each expression after entering the one
        # before it, so reading it inside would size the clock off 1000.0 and
        # release the pass on message three — green, and for the wrong reason.
        step = _midi.MAX_DRAIN_WORK_S / 3
        with (
            mock.patch.object(_midi, "MAX_DRAIN_WORK_S", 1000.0),
            mock.patch.object(_midi, "_monotonic", self._clock(step)),
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
        with mock.patch.object(_midi, "_monotonic", self._clock(_midi.MAX_DRAIN_WORK_S / 3)):
            for _ in range(20):
                seen.extend(_midi.poll_pending(port, stop))
        self.assertEqual(seen, [f"msg{i}" for i in range(1, 21)])

    def test_the_default_work_budget_is_a_fraction_of_the_flush_period(self):
        # The relationship, against the two scene constants that define the
        # period rather than a literal copy of them: `MAX_DRAIN_WORK_S` exists
        # to keep a pass from eating the flush that follows it, and a pass free
        # to spend the whole period would halve the flush rate rather than
        # bound it. This holds for the *default*, which is AsidScene's budget
        # — its `_handle_sysex` pokes a shadow and returns. MidiScene passes
        # its own and does not; the sibling test below is where that lives.
        from c64cast.sid import asid_scene, midi_scene

        protected_s = min(asid_scene._FLUSH_INTERVAL_S, midi_scene._CONTROL_FLUSH_INTERVAL_S)
        self.assertGreater(_midi.MAX_DRAIN_WORK_S, 0.0)
        self.assertLess(_midi.MAX_DRAIN_WORK_S, protected_s)

    def test_midi_scenes_own_budget_overruns_that_fraction_on_a_slow_link(self):
        # The invariant above is the default's, not the system's, and an
        # assertion that only checked the default read as though it covered
        # both callers. MidiScene's reader passes `_drain_budget_s`, which
        # widens until a worst-case chord retires in one pass; on an Ultimate
        # that is 31.332 ms against a 16.667 ms flush period — 1.88x the
        # fraction the default is held to.
        #
        # That is a deliberate latency trade, not a missing bound: the chord's
        # notes land together and the wheel/CC flush after that one pass is
        # late by the difference, because the flush check sits after the drain
        # and is itself rate-limited. Pinned with the ratio so that retuning
        # any of the four constants behind it — the flush period, the write
        # cost model, `_WRITES_PER_NOTE`, `_NOTES_PER_DRAIN` — has to come
        # past this assertion and say so.
        #
        # Against `_CONTROL_FLUSH_INTERVAL_S` alone, not the `min()` the
        # sibling test takes: this is MidiScene's own overrun of MidiScene's
        # own flush period, and a `min()` over both scenes' intervals leaves
        # whichever one is larger unpinned.
        from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE
        from c64cast.sid import midi_scene

        protected_s = midi_scene._CONTROL_FLUSH_INTERVAL_S
        ultimate_s = midi_scene._drain_budget_s(ULTIMATE_PROFILE)
        self.assertAlmostEqual(ultimate_s / protected_s, 1.88, places=2)

        # A link whose writes are cheap keeps the default, and so keeps the
        # invariant: the overrun is the slow link's, not every link's.
        self.assertEqual(midi_scene._drain_budget_s(TEENSYROM_PROFILE), _midi.MAX_DRAIN_WORK_S)

    def test_the_shared_default_is_midi_scenes_floor_not_its_budget(self):
        # config.md named `midi_scene.MAX_DRAIN_WORK_S` as the lever a test
        # reaches for once it has learned that rebinding `_midi`'s own copy is
        # inert for MidiScene. It is a `max()` operand, so it only moves the
        # answer from above the profile-derived term — and on an Ultimate that
        # term is 31.332 ms, so rebinding the copy *down* is a second silent
        # no-op, in the one direction someone wanting a one-message pass would
        # try. Which operand wins is the fact worth pinning; the numbers the
        # sibling test above already holds.
        from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE
        from c64cast.sid import midi_scene

        ultimate_s = midi_scene._drain_budget_s(ULTIMATE_PROFILE)
        with mock.patch.object(midi_scene, "MAX_DRAIN_WORK_S", 0.0):
            self.assertEqual(midi_scene._drain_budget_s(ULTIMATE_PROFILE), ultimate_s)
            # The cheap link is where the constant is the operative term, so
            # the same rebind does move it — down to that link's derived floor
            # and not to the zero, which is what makes it a floor and not a
            # budget.
            teensy_s = midi_scene._drain_budget_s(TEENSYROM_PROFILE)
        self.assertLess(teensy_s, _midi.MAX_DRAIN_WORK_S)
        self.assertGreater(teensy_s, 0.0)

        # From above, both move: the operand that wins is decided per call, not
        # per profile, so a wide enough rebind is the lever the doc claimed.
        with mock.patch.object(midi_scene, "MAX_DRAIN_WORK_S", 1.0):
            self.assertEqual(midi_scene._drain_budget_s(ULTIMATE_PROFILE), 1.0)
            self.assertEqual(midi_scene._drain_budget_s(TEENSYROM_PROFILE), 1.0)


if __name__ == "__main__":
    unittest.main()
