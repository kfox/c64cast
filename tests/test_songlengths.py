"""Tests for c64cast.sid.songlengths — the HVSC Songlengths.md5 lookup."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest


class SongLengthsTest(unittest.TestCase):
    def test_parse_and_lookup(self):
        from c64cast.sid.songlengths import LengthsDB, md5_of_sid

        # Build a minimal SID; HVSC keys Songlengths.md5 by a plain MD5 of
        # the whole file (header included), not just the data payload.
        header = bytearray(124)
        header[0:4] = b"PSID"
        header[6:8] = (0x7C).to_bytes(2, "big")  # data_offset = 124
        header[14:16] = (3).to_bytes(2, "big")
        data_payload = b"\x12\x34" * 32
        sid_bytes = bytes(header) + data_payload
        expected_md5 = hashlib.md5(sid_bytes).hexdigest()

        with tempfile.NamedTemporaryFile("w", suffix=".md5", delete=False) as f:
            f.write("; comment\n")
            f.write(f"{expected_md5}=1:23 2:34 0:30.500\n")
            path = f.name
        try:
            db = LengthsDB.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(md5_of_sid(sid_bytes), expected_md5)
        s1 = db.lookup(sid_bytes, 1)
        s2 = db.lookup(sid_bytes, 2)
        s3 = db.lookup(sid_bytes, 3)
        assert s1 is not None and s2 is not None and s3 is not None
        self.assertAlmostEqual(s1, 83.0)
        self.assertAlmostEqual(s2, 154.0)
        self.assertAlmostEqual(s3, 30.5)
        self.assertIsNone(db.lookup(sid_bytes, 99))

    def test_unknown_sid_returns_none(self):
        from c64cast.sid.songlengths import LengthsDB

        with tempfile.NamedTemporaryFile("w", suffix=".md5", delete=False) as f:
            f.write("aaaa=1:00\n")
            path = f.name
        try:
            db = LengthsDB.load(path)
        finally:
            os.unlink(path)
        sid = b"PSID" + b"\x00" * 124
        self.assertIsNone(db.lookup(sid, 1))


if __name__ == "__main__":
    unittest.main()
