"""Direct contract tests for c64cast._native_io.silence_native_stderr.

No prior test file imported this module at all: every failure mode here
presents as the *absence* of stderr output, which is indistinguishable from
"nothing went wrong" without a test that watches fd 2 directly. So every
test below repoints the real fd 2 at a pipe for its duration (never at
sys.stderr the Python object — this module bypasses that entirely) and
restores it via addCleanup, in LIFO order, so a mid-test assertion failure
still leaves fd 2 exactly as the test found it.
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from c64cast._native_io import silence_native_stderr


class _Fd2PipeTestCase(unittest.TestCase):
    """Points real fd 2 at a pipe so a test can observe raw `os.write(2, ...)`
    without touching the test runner's own stderr."""

    def setUp(self):
        self.read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, self.read_fd)
        saved = os.dup(2)
        self.addCleanup(os.close, saved)
        self.addCleanup(os.dup2, saved, 2)
        os.dup2(write_fd, 2)
        os.close(write_fd)

    def read_all(self) -> bytes:
        """Everything written to fd 2 so far, without blocking past it."""
        os.set_blocking(self.read_fd, False)
        try:
            return os.read(self.read_fd, 65536)
        except BlockingIOError:
            return b""


class SilenceNativeStderrTest(_Fd2PipeTestCase):
    def test_writes_inside_the_block_are_silenced(self):
        with silence_native_stderr():
            os.write(2, b"muted")
        self.assertEqual(self.read_all(), b"")

    def test_writes_after_the_block_are_not(self):
        with silence_native_stderr():
            pass
        os.write(2, b"heard")
        self.assertEqual(self.read_all(), b"heard")

    def test_nested_use_restores_only_after_the_outermost_exit(self):
        with silence_native_stderr():
            with silence_native_stderr():
                os.write(2, b"inner")
            os.write(2, b"still muted")
        os.write(2, b"heard")
        self.assertEqual(self.read_all(), b"heard")

    def test_no_fd_leak_across_many_enter_exit_cycles(self):
        # A leak on either failure path (saved outside the try, devnull's
        # close after a dup2 that could raise) shows up as fd numbers
        # climbing — a probe fd opened before and after should land on the
        # same number, since the OS hands out the lowest free one.
        probe = os.open(os.devnull, os.O_RDONLY)
        os.close(probe)
        for _ in range(200):
            with silence_native_stderr():
                pass
        probe_after = os.open(os.devnull, os.O_RDONLY)
        os.close(probe_after)
        self.assertEqual(probe_after, probe)

    def test_devnull_open_failure_leaks_nothing_and_propagates(self):
        probe = os.open(os.devnull, os.O_RDONLY)
        os.close(probe)
        with mock.patch("os.open", side_effect=OSError("EMFILE")):
            with self.assertRaises(OSError):
                with silence_native_stderr():
                    pass
        # fd 2 must still be the pipe's write end — the failed attempt never
        # touched it — and the `saved` dup from the failed attempt must be closed.
        os.write(2, b"still wired to the pipe")
        self.assertEqual(self.read_all(), b"still wired to the pipe")
        probe_after = os.open(os.devnull, os.O_RDONLY)
        os.close(probe_after)
        self.assertEqual(probe_after, probe)

    def test_a_failed_enter_does_not_wedge_a_later_call(self):
        with mock.patch("os.open", side_effect=OSError("EMFILE")):
            with self.assertRaises(OSError):
                with silence_native_stderr():
                    pass
        # The depth counter must have unwound from the failed attempt, or
        # this call would see itself as "not first" and never redirect.
        with silence_native_stderr():
            os.write(2, b"muted")
        self.assertEqual(self.read_all(), b"")

    def test_overlapping_non_nested_calls_restore_real_stderr_once_both_exit(self):
        # The exact shape of the reported bug: entry order A, B and exit
        # order A, B (B — the *second* entrant — is also the *last* exiter).
        # The buggy version had B's own `os.dup(2)` capture A's /dev/null
        # redirect as its "saved" fd, so B's exit pinned fd 2 to /dev/null
        # for good. The depth counter must make B's entry a no-op instead.
        a_entered = threading.Event()
        b_entered = threading.Event()
        a_exited = threading.Event()
        failures: list[BaseException] = []

        def worker_a():
            try:
                with silence_native_stderr():
                    a_entered.set()
                    self.assertTrue(b_entered.wait(2.0))
            except BaseException as e:  # noqa: BLE001 - the assertion is the raise
                failures.append(e)
            finally:
                a_exited.set()

        def worker_b():
            try:
                self.assertTrue(a_entered.wait(2.0))
                with silence_native_stderr():
                    b_entered.set()
                    self.assertTrue(a_exited.wait(2.0))
            except BaseException as e:  # noqa: BLE001 - the assertion is the raise
                failures.append(e)

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        tb.start()
        ta.join(5.0)
        tb.join(5.0)

        self.assertEqual(failures, [])
        os.write(2, b"heard")
        self.assertEqual(self.read_all(), b"heard")


if __name__ == "__main__":
    unittest.main()
