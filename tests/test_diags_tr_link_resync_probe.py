"""tr_link_resync_probe's den0 case passes only a run that actually blanked the display."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from test_diags_nmi_latch import _load

from c64cast.hw.teensyrom_dma import TRError


class _FakeTR:
    def __init__(self, d011_write_fails: bool = False) -> None:
        self.mem = bytearray(0x10000)
        self.mem[0xD011] = 0x1B
        self.mem[0xFFFC:0xFFFE] = b"\xe2\xfc"
        self.d011_write_fails = d011_write_fails

    def write_segment(self, addr: int, data: bytes) -> None:
        if addr == 0xD011 and self.d011_write_fails:
            raise TRError("WriteC64Mem $D011: TR timed out receiving the command")
        self.mem[addr : addr + len(data)] = data

    def read_segment(self, addr: int, length: int) -> bytes:
        return bytes(self.mem[addr : addr + length])

    def ping(self) -> str:
        return "TeensyROM is on-line"

    def reset(self) -> None:
        pass

    def drain_after_command(self, quiet_s: float = 0.2) -> str:
        return ""


class Den0VerdictTest(unittest.TestCase):
    def _run(self, tr: _FakeTR) -> tuple[bool, str]:
        probe = _load("tr_link_resync_probe")
        buf = io.StringIO()
        with patch.object(probe.time, "sleep"), redirect_stdout(buf):
            ok = probe.case_den0(tr)
        return ok, buf.getvalue()

    def test_a_run_that_cleared_den_passes(self) -> None:
        ok, out = self._run(_FakeTR())
        self.assertTrue(ok)
        self.assertIn("0 failed, 0 read back wrong", out)

    def test_a_run_whose_d011_write_failed_does_not_pass(self) -> None:
        ok, out = self._run(_FakeTR(d011_write_fails=True))
        self.assertFalse(ok)
        self.assertIn("DEN read back clear: False", out)


if __name__ == "__main__":
    unittest.main()
