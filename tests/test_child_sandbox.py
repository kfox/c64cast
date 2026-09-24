"""The clamp that holds every child the test process waits on to `BOUND_S`.

Two halves, like `tests/test_child_process.py`. The clamp itself — which waits
it shortens, which it leaves to the caller, and that what comes out cannot be
caught by an `except Exception`. And the two production probes it exists for,
driven against a command that really does wedge, because a guard whose only
evidence is its own unit tests is a guard nobody has watched work.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

import _child_process
import _child_sandbox
from _child_sandbox import ChildProcessHung

from c64cast.app import doctor, upgrade

#: Short enough that driving a real expiry costs a fraction of a second.
_TEST_BOUND_S = 0.3

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_NO_SHELL_STAND_IN = "needs a single-process stand-in on PATH; a .bat would orphan its child"


def _hangs() -> list[str]:
    """A child that never exits.

    300 s rather than a bare block: if the clamp under test ever stopped
    working, this fails the per-test cap in a minute instead of holding the
    worker until CI gives up on the whole job.
    """
    return [sys.executable, "-c", "import time; time.sleep(300)"]


def _wedge_on_path(name: str, says: str) -> str:
    """Put a `name` on PATH that writes `says` to stderr and never returns.

    Returns the directory to prepend. A shell `echo` rather than a Python
    one-liner, so the write lands in milliseconds and `_TEST_BOUND_S` covers
    it without the interpreter-startup margin `tests/test_child_process.py`
    has to leave. `exec` so the process the suite kills is the one sleeping —
    a wrapper that forked would leave the sleeper behind, which is the shape
    these tests are about.
    """
    directory = tempfile.mkdtemp()
    launcher = os.path.join(directory, name)
    with open(launcher, "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\necho {shlex.quote(says)} >&2\nexec sleep 300\n")
    os.chmod(launcher, 0o755)
    return directory


def _first_on_path(directory: str) -> Any:
    return mock.patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ["PATH"]})


def _load_script(name: str) -> Any:
    """`scripts/<name>.py` as a module, the way tests/test_prose_gate.py loads it."""
    path = os.path.join(_REPO_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ArmedTest(unittest.TestCase):
    def test_the_suite_runs_with_the_clamp_armed(self):
        # Every other test here patches `BOUND_S` and drives the wrappers, so
        # they would all pass in a run where `sitecustomize` never installed
        # them and nothing was clamped at all.
        self.assertTrue(_child_sandbox._armed)


class ClampTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(_child_process, "BOUND_S", _TEST_BOUND_S)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_wait_longer_than_the_suites_bound_is_cut_short_and_named(self):
        with self.assertRaises(ChildProcessHung) as caught:
            subprocess.run(_hangs(), timeout=60, capture_output=True)
        message = str(caught.exception)
        self.assertIn(f"did not exit within {_TEST_BOUND_S:g}s", message)
        self.assertIn("time.sleep(300)", message)
        self.assertIn("the caller allowed 60s", message)

    def test_a_tighter_bound_is_left_to_the_caller(self):
        # The caller's own number is what its own tests grade; converting it
        # would grade this module instead.
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(_hangs(), timeout=_TEST_BOUND_S / 5, capture_output=True)

    def test_a_bound_equal_to_the_suites_is_left_to_the_caller(self):
        # `upgrade._stop`'s interrupt grace is exactly `BOUND_S`.
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(_hangs(), timeout=_TEST_BOUND_S, capture_output=True)

    def test_an_except_exception_around_the_call_cannot_swallow_it(self):
        # Both production sites catch their own expiry and degrade to a
        # warning. An ordinary exception here would land in one of those and
        # the test would go green over a command that never returned.
        def swallows() -> str:
            try:
                subprocess.run(_hangs(), timeout=60, capture_output=True)
            except Exception:
                return "could not check"
            return "ran"

        with self.assertRaises(ChildProcessHung):
            swallows()

    def test_the_child_is_killed_before_the_failure_is_raised(self):
        popen = mock.Mock(args=["sleep", "300"])
        expired = subprocess.TimeoutExpired(popen.args, _TEST_BOUND_S)
        error = _child_sandbox._hung(popen, _TEST_BOUND_S, 60.0, expired)
        popen.kill.assert_called_once_with()
        self.assertIn("sleep", str(error))


class ProductionChildTest(unittest.TestCase):
    """The children this module exists for: started by the code under test."""

    def test_an_unbounded_production_wait_is_cut_short_and_named(self):
        # `$C64CAST_UPGRADE_TIMEOUT_S=0` is the documented escape hatch that
        # makes `_run_command` wait indefinitely — a real `Popen.wait(None)`,
        # which is also the shape every `Popen.__exit__` in the suite takes.
        with (
            mock.patch.dict(os.environ, {upgrade.UPGRADE_TIMEOUT_ENV: "0"}),
            mock.patch.object(_child_process, "BOUND_S", _TEST_BOUND_S),
            self.assertRaises(ChildProcessHung) as caught,
        ):
            upgrade._run_command(_hangs())
        message = str(caught.exception)
        self.assertIn("time.sleep(300)", message)
        self.assertIn("the caller allowed no bound at all", message)

    @unittest.skipIf(os.name == "nt", _NO_SHELL_STAND_IN)
    def test_a_wedged_uv_is_named_rather_than_swallowed_into_a_warning(self):
        # #496. `_probe_uv_lock` bounds `uv lock --check` at 60 s — the same
        # number as the per-test cap — and catches the expiry into a `warn`
        # Diagnostic, so before the clamp a wedged `uv` cost 60 s and reported
        # either "no progress" or "could not check", never the command.
        directory = _wedge_on_path("uv", "Resolving dependencies")
        with (
            _first_on_path(directory),
            mock.patch.object(_child_process, "BOUND_S", _TEST_BOUND_S),
            self.assertRaises(ChildProcessHung) as caught,
        ):
            doctor._probe_uv_lock()
        message = str(caught.exception)
        self.assertIn("['uv', 'lock', '--check']", message)
        self.assertIn("stderr: Resolving dependencies", message)

    @unittest.skipIf(os.name == "nt", _NO_SHELL_STAND_IN)
    def test_a_wedged_git_under_the_prose_gate_is_named_too(self):
        # The second site with the same shape: `_DIFF_TIMEOUT_S` is 60 as
        # well, and the expiry is caught into an empty result.
        lint_comments = _load_script("lint_comments")
        directory = _wedge_on_path("git", "Enumerating objects")
        with (
            _first_on_path(directory),
            mock.patch.object(_child_process, "BOUND_S", _TEST_BOUND_S),
            self.assertRaises(ChildProcessHung) as caught,
        ):
            lint_comments.added_lines(["m.py"])
        self.assertIn("'diff', '--cached'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
