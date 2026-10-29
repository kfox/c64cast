"""halt_shape_probe --store-timing writes the way playback does on every backend."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_diags_nmi_latch import _load


class StoreTimingNotesConsumerTest(unittest.TestCase):
    def test_background_writes_run_with_the_nmi_consumer_noted(self) -> None:
        # A TeensyROM backend slices only while an NMI consumer is noted, so
        # without it --tr-slicing is ignored for every timed write.
        probe = _load("halt_shape_probe")
        be = MagicMock()
        noted_during_trials: list[bool] = []

        def trial(*_args, **_kwargs):
            calls = [c.args[0] for c in be.note_nmi_consumer.call_args_list]
            noted_during_trials.append(bool(calls) and calls[-1] is True)
            return None

        out_dir = Path(tempfile.mkdtemp())
        args = argparse.Namespace(
            nmi_rate=12000,
            system="NTSC",
            hold_payload=128,
            write_rate_timing=30.0,
            trials=2,
            store_timing=True,
        )
        buf = io.StringIO()
        with (
            patch.object(probe, "setup"),
            patch.object(probe, "store_timing_trial", side_effect=trial),
            patch.object(probe.d, "stamped", return_value=out_dir / "st.json"),
            redirect_stdout(buf),
        ):
            probe.store_timing(be, args)

        self.assertIn("read-back failed", buf.getvalue())
        self.assertEqual(noted_during_trials, [True, True])
        self.assertIs(be.note_nmi_consumer.call_args_list[-1].args[0], False)


class StoreTimingArmsBelowTheFloorTest(unittest.TestCase):
    def test_a_rate_past_the_safe_ceiling_arms_its_own_latch(self) -> None:
        probe = _load("halt_shape_probe")
        out_dir = Path(tempfile.mkdtemp())
        args = argparse.Namespace(
            nmi_rate=13636,
            system="NTSC",
            hold_payload=128,
            write_rate_timing=0.0,
            trials=1,
            store_timing=True,
        )
        with (
            patch.object(probe, "setup"),
            patch.object(probe, "store_timing_trial", return_value=None) as trial,
            patch.object(probe.d, "stamped", return_value=out_dir / "st.json"),
            redirect_stdout(io.StringIO()),
        ):
            probe.store_timing(MagicMock(), args)

        self.assertEqual(trial.call_args.args[1], 74)


class StoreTimingSetupReportsTheArmedPeriodTest(unittest.TestCase):
    def test_the_setup_line_names_the_period_store_timing_arms(self) -> None:
        # 13636 Hz NTSC is latch 74 (period 75), under the safe floor that
        # playback clamps to; --store-timing arms it unclamped.
        probe = _load("halt_shape_probe")
        argv = ["halt_shape_probe.py", "--store-timing", "--nmi-rate", "13636"]
        buf = io.StringIO()
        with (
            patch.object(probe.sys, "argv", argv),
            patch.object(probe, "make_backend"),
            patch.object(probe, "store_timing", return_value=0),
            redirect_stdout(buf),
        ):
            self.assertEqual(probe.main(), 0)
        self.assertIn("75 cycles/tick", buf.getvalue())


class StoreTimingRefusesLockedSlicingTest(unittest.TestCase):
    def test_a_lock_capable_backend_is_refused_before_any_trial(self) -> None:
        probe = _load("halt_shape_probe")
        be = MagicMock(_lock_capable=True)
        args = argparse.Namespace(
            nmi_rate=12000,
            system="NTSC",
            hold_payload=128,
            write_rate_timing=30.0,
            trials=2,
            store_timing=True,
        )
        buf = io.StringIO()
        with (
            patch.object(probe, "setup") as setup,
            patch.object(probe, "store_timing_trial") as trial,
            redirect_stdout(buf),
        ):
            self.assertEqual(probe.store_timing(be, args), 2)
        self.assertIn("cannot time locked slicing", buf.getvalue())
        setup.assert_not_called()
        trial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
