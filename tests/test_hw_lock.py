"""scripts/diags/hw_lock.py: one command at a time per device, and the
command's exit code comes back as the tool's own."""

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
            self.assertEqual(hw_lock.lock_path("u64://Host"), Path("/x/locks/host.lock"))


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
        self.env = {**os.environ, "C64_DIAG_LOCK_DIR": str(self.tmp / "locks")}
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

    def test_a_nested_call_for_the_same_device_runs_without_waiting(self) -> None:
        result = self._run(sys.executable, str(_SCRIPT), sys.executable, "-c", "pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("waiting", result.stderr)

    def test_waits_for_the_holder_and_not_for_another_device(self) -> None:
        import fcntl

        released = self.tmp / "released"
        probe = f"import os, sys; sys.exit(0 if os.path.exists({str(released)!r}) else 3)"
        path = hw_lock.lock_path("u64://rig")
        path.parent.mkdir(parents=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

        other = self._run("--device", "u64://other-rig", sys.executable, "-c", "pass")
        self.assertEqual(other.returncode, 0)
        self.assertNotIn("waiting", other.stderr)

        log_path = self.tmp / "waiter.log"
        outcome: list[subprocess.CompletedProcess[str]] = []
        with open(log_path, "w") as log:
            waiter = threading.Thread(
                target=lambda: outcome.append(
                    self._run("--device", "http://rig", sys.executable, "-c", probe, log=log)
                )
            )
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
                fcntl.flock(fd, fcntl.LOCK_UN)
                waiter.join()

        self.assertEqual(outcome[0].returncode, 0, log_path.read_text())
        self.assertIn("acquired", log_path.read_text())


if __name__ == "__main__":
    unittest.main()
