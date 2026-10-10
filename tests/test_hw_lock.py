"""scripts/diags/hw_lock.py: one command at a time per rig, whatever
``--device`` spelling each caller uses, and the command's exit code comes back
as the tool's own."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType
from typing import IO, Any
from unittest.mock import patch

from _child_process import run_bounded

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diags" / "hw_lock.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hw_lock", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hw_lock = _load()


class LockKeyTest(unittest.TestCase):
    def test_url_spellings_of_one_host_share_a_key(self) -> None:
        keys = {
            hw_lock.lock_key(u)
            for u in ("u64://192.168.2.64", "http://192.168.2.64:80", "192.168.2.64")
        }
        self.assertEqual(keys, {"192.168.2.64"})

    def test_a_url_without_a_host_keys_on_the_whole_string(self) -> None:
        self.assertEqual(hw_lock.lock_key("tr:///dev/cu.usbmodem1"), "tr_dev_cu.usbmodem1")

    def test_a_malformed_url_keys_on_the_whole_string(self) -> None:
        self.assertEqual(hw_lock.lock_key("http://[bad"), "http_bad")

    def test_lock_dir_override(self) -> None:
        with patch.dict(os.environ, {"C64_DIAG_LOCK_DIR": "/x/locks"}):
            self.assertEqual(hw_lock.lock_dir(), Path("/x/locks"))


class LockPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"C64_DIAG_LOCK_DIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(hw_lock.RIGS_ENV, None)
        os.environ.pop(hw_lock.HELD_ENV, None)

    def test_every_spelling_takes_the_one_rig_lock_without_a_map(self) -> None:
        for device in (None, "u64://192.168.2.64", "http://192.168.2.64", "Cam Link", "tr://"):
            with self.subTest(device=device):
                self.assertEqual(hw_lock.lock_paths(device), [self.dir / "default.lock"])

    def test_a_legacy_lock_file_is_taken_too_in_sorted_order(self) -> None:
        for name in ("cam_link", "192.168.2.64"):
            (self.dir / f"{name}.lock").touch()
        expected = [self.dir / f"{n}.lock" for n in ("192.168.2.64", "cam_link", "default")]
        self.assertEqual(hw_lock.lock_paths("Cam Link"), expected)

    def test_a_mapped_device_takes_only_its_rigs_lock(self) -> None:
        rigs = "a=u64://192.168.2.64,Cam Link; b=http://192.168.2.65"
        with patch.dict(os.environ, {hw_lock.RIGS_ENV: rigs}):
            self.assertEqual(hw_lock.lock_paths("http://192.168.2.64:80"), [self.dir / "a.lock"])
            self.assertEqual(hw_lock.lock_paths("cam link"), [self.dir / "a.lock"])
            self.assertEqual(hw_lock.lock_paths("u64://192.168.2.65"), [self.dir / "b.lock"])

    def test_a_mapped_device_takes_its_rigs_legacy_files_too(self) -> None:
        for name in ("192.168.2.64", "cam_link", "192.168.2.65", "default"):
            (self.dir / f"{name}.lock").touch()
        rigs = "a=u64://192.168.2.64,Cam Link;b=u64://192.168.2.65"
        expected = [self.dir / f"{n}.lock" for n in ("192.168.2.64", "a", "cam_link")]
        with patch.dict(os.environ, {hw_lock.RIGS_ENV: rigs}):
            self.assertEqual(hw_lock.lock_paths("Cam Link"), expected)

    def test_an_unopenable_lock_file_refuses_to_run(self) -> None:
        (self.dir / "stray.lock").mkdir()
        err = io.StringIO()
        with (
            patch.object(hw_lock.os, "execvp", side_effect=AssertionError("ran anyway")),
            patch.object(sys, "platform", "linux"),
            redirect_stderr(err),
        ):
            self.assertEqual(hw_lock.main(["true"]), 2)
        self.assertIn("stray.lock", err.getvalue())

    def test_an_unmapped_device_takes_every_rigs_lock(self) -> None:
        with patch.dict(os.environ, {hw_lock.RIGS_ENV: "b=u64://h2;a=u64://h1"}):
            expected = [self.dir / f"{n}.lock" for n in ("a", "b", "default")]
            self.assertEqual(hw_lock.lock_paths(None), expected)
            self.assertEqual(hw_lock.lock_paths("Cam Link"), expected)

    def test_a_malformed_map_is_an_error(self) -> None:
        for spec in ("u64://h1", "=u64://h1", "a=", "a=h1;b=h1"):
            with self.subTest(spec=spec), self.assertRaises(hw_lock.RigMapError):
                hw_lock.parse_rigs(spec)

    def test_a_malformed_map_refuses_to_run(self) -> None:
        err = io.StringIO()
        with (
            patch.dict(os.environ, {hw_lock.RIGS_ENV: "nonsense"}),
            patch.object(hw_lock.os, "execvp", side_effect=AssertionError("ran anyway")),
            patch.object(sys, "platform", "linux"),
            redirect_stderr(err),
        ):
            self.assertEqual(hw_lock.main(["true"]), 2)
        self.assertIn(hw_lock.RIGS_ENV, err.getvalue())


class WindowsTest(unittest.TestCase):
    def test_refuses_with_a_message(self) -> None:
        err = io.StringIO()
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(os.environ, {"C64_DIAG_LOCK_DIR": tempfile.mkdtemp()}),
            patch.object(hw_lock.os, "execvp", side_effect=AssertionError("ran past the guard")),
            redirect_stderr(err),
        ):
            self.assertEqual(hw_lock.main(["true"]), 2)
        self.assertIn("POSIX only", err.getvalue())


@unittest.skipIf(sys.platform == "win32", "hw_lock is POSIX only")
class RunUnderLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # A suite run under hw_lock exports its own lock; these children must not inherit it.
        inherited = {hw_lock.HELD_ENV, hw_lock.RIGS_ENV}
        self.env = {k: v for k, v in os.environ.items() if k not in inherited}
        self.env["C64_DIAG_LOCK_DIR"] = str(self.tmp / "locks")
        env_patch = patch.dict(os.environ, {"C64_DIAG_LOCK_DIR": self.env["C64_DIAG_LOCK_DIR"]})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, *argv: str, log: IO[str] | None = None) -> subprocess.CompletedProcess[str]:
        streams: dict[str, Any] = (
            {"stdout": log, "stderr": log} if log else {"capture_output": True}
        )
        return run_bounded(
            [sys.executable, str(_SCRIPT), *argv], env=self.env, text=True, **streams
        )

    def test_exit_code_is_the_commands(self) -> None:
        result = self._run(sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_missing_command_exits_127(self) -> None:
        result = self._run(str(self.tmp / "no-such-command"))
        self.assertEqual(result.returncode, 127)
        self.assertIn("cannot run", result.stderr)

    def test_a_nested_call_for_another_device_runs_without_waiting(self) -> None:
        inner = [sys.executable, str(_SCRIPT), "--device", "Cam Link", sys.executable, "-c", "pass"]
        result = self._run("--device", "u64://rig", *inner)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("waiting", result.stderr)

    def test_a_nested_call_for_another_rig_refuses_to_run(self) -> None:
        self.env[hw_lock.RIGS_ENV] = "a=u64://rig;b=u64://other-rig"
        inner = [sys.executable, str(_SCRIPT), "--device", "u64://other-rig", "true"]
        result = self._run("--device", "u64://rig", *inner)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("nested call needs", result.stderr)

    def _assert_waits(self, hold: Callable[[], Any], *waiter_argv: str) -> None:
        """While ``hold`` holds a lock, a call for ``waiter_argv`` waits, then runs."""
        released = self.tmp / "released"
        probe = f"import os, sys; sys.exit(0 if os.path.exists({str(released)!r}) else 3)"
        log_path = self.tmp / "waiter.log"
        outcome: list[subprocess.CompletedProcess[str]] = []
        with open(log_path, "w") as log:
            waiter = threading.Thread(
                target=lambda: outcome.append(
                    self._run(*waiter_argv, sys.executable, "-c", probe, log=log)
                )
            )
            release = hold()
            waiter.start()
            try:
                deadline = time.monotonic() + 15
                while "waiting for" not in log_path.read_text() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn("waiting for", log_path.read_text())
                waiter.join(timeout=1.0)
                self.assertTrue(waiter.is_alive(), "the command ran while the lock was held")
            finally:
                released.touch()
                release()
                waiter.join()
        self.assertEqual(outcome[0].returncode, 0, log_path.read_text())
        self.assertIn("acquired", log_path.read_text())

    def _hold_with_hw_lock(self, *argv: str) -> Callable[[], Callable[[], None]]:
        """A real hw_lock process holding its lock until the test releases it."""

        def hold() -> Callable[[], None]:
            held, done = self.tmp / "held", self.tmp / "holder-done"
            wait = (
                "import os, time, pathlib\n"
                f"pathlib.Path({str(held)!r}).touch()\n"
                "deadline = time.monotonic() + 15\n"
                f"while not os.path.exists({str(done)!r}) and time.monotonic() < deadline:\n"
                "    time.sleep(0.02)\n"
            )
            holder = threading.Thread(target=self._run, args=(*argv, sys.executable, "-c", wait))
            holder.start()
            deadline = time.monotonic() + 15
            while not held.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(held.exists(), "the holder never started")

            def release() -> None:
                done.touch()
                holder.join()

            return release

        return hold

    def test_different_device_spellings_contend_for_one_lock(self) -> None:
        cases = [
            ((), ("--device", "u64://192.168.2.64")),
            (("--device", "u64://192.168.2.64"), ("--device", "Cam Link")),
            (("--device", "Cam Link"), ()),
        ]
        for holder_argv, waiter_argv in cases:
            with self.subTest(holder=holder_argv, waiter=waiter_argv):
                for stale in ("released", "held", "holder-done"):
                    (self.tmp / stale).unlink(missing_ok=True)
                self._assert_waits(self._hold_with_hw_lock(*holder_argv), *waiter_argv)

    def _hold_with_flock(self, name: str) -> Callable[[], Callable[[], None]]:
        """An old-version caller: one flock on ``name``.lock and nothing else."""
        assert sys.platform != "win32"  # the class skips there; this narrows fcntl for pyright
        import fcntl

        def hold() -> Callable[[], None]:
            path = hw_lock.lock_dir() / f"{name}.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT)
            self.addCleanup(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            return lambda: fcntl.flock(fd, fcntl.LOCK_UN)

        return hold

    def test_waits_for_a_legacy_holder_of_a_per_device_key(self) -> None:
        self._assert_waits(self._hold_with_flock("192.168.2.64"))

    def test_a_mapped_rig_does_not_wait_for_another_rig(self) -> None:
        self.env[hw_lock.RIGS_ENV] = "a=u64://rig;b=u64://other-rig"
        release = self._hold_with_flock("a")()
        self.addCleanup(release)
        other = self._run("--device", "u64://other-rig", sys.executable, "-c", "pass")
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertNotIn("waiting", other.stderr)

    def test_an_unmapped_device_waits_for_a_mapped_rig(self) -> None:
        self.env[hw_lock.RIGS_ENV] = "a=u64://rig"
        self._assert_waits(self._hold_with_flock("a"), "--device", "Cam Link")


if __name__ == "__main__":
    unittest.main()
