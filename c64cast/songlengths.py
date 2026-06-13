"""HVSC SongLengths database lookup.

The High Voltage SID Collection ships a `Songlengths.md5` file mapping
the MD5 of each SID file to a list of per-subtune durations. Format:

    ; comment lines start with `;`
    <32-char-md5>=<dur1> <dur2> <dur3> ...

Each `dur` is "M:SS" or "M:SS.mmm". Subtune 1 → durations[0], etc.

We compute the MD5 over the SID file's data area only (per HVSC spec —
the data starts at the `data_offset` field of the PSID header so users
who rewrite metadata fields don't break the lookup). For RSID files the
spec is the same.

Usage:
    from c64cast.songlengths import LengthsDB, song_length
    db = LengthsDB.load("Songlengths.md5")
    secs = db.lookup(sid_bytes, song=1)   # → float or None
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_DUR_RE = re.compile(r"(\d+):(\d+)(?:\.(\d+))?")


def _parse_duration(text: str) -> float | None:
    """Parse an HVSC duration string like '2:34', '0:30.500', or '12:05'.

    Returns seconds as float, or None on parse failure."""
    m = _DUR_RE.match(text.strip())
    if not m:
        return None
    minutes = int(m.group(1))
    seconds = int(m.group(2))
    millis = int((m.group(3) or "0").ljust(3, "0")[:3])
    return minutes * 60.0 + seconds + millis / 1000.0


def md5_of_sid(sid_bytes: bytes) -> str:
    """Return the HVSC-style MD5 hex digest of a SID file's data payload.

    Per the HVSC spec, the hash covers the SID's loaded data only — not
    the PSID/RSID header fields. The header's `data_offset` (bytes 6-7,
    big-endian) tells us where the payload starts."""
    if len(sid_bytes) < 8:
        raise ValueError("SID file too short for header")
    data_offset = int.from_bytes(sid_bytes[6:8], "big")
    if data_offset >= len(sid_bytes):
        raise ValueError(
            f"SID data_offset {data_offset} >= file size {len(sid_bytes)}")
    return hashlib.md5(sid_bytes[data_offset:]).hexdigest()


@dataclass
class LengthsDB:
    """Loaded HVSC Songlengths.md5 mapping md5_hex → list of per-subtune
    seconds. ``None`` entries inside the list mean "duration unknown for
    that subtune"."""
    entries: dict[str, list[float | None]]

    @classmethod
    def load(cls, path: str) -> LengthsDB:
        if not os.path.exists(path):
            raise FileNotFoundError(f"SongLengths file not found: {path}")
        entries: dict[str, list[float | None]] = {}
        with open(path, encoding="ascii", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(";"):
                    continue
                if "=" not in line:
                    continue
                md5_hex, durs_str = line.split("=", 1)
                md5_hex = md5_hex.strip().lower()
                if len(md5_hex) != 32:
                    continue
                durs: list[float | None] = []
                for tok in durs_str.split():
                    durs.append(_parse_duration(tok))
                entries[md5_hex] = durs
        log.info("songlengths: loaded %d entries from %s",
                 len(entries), path)
        return cls(entries=entries)

    def lookup(self, sid_bytes: bytes, song: int = 1) -> float | None:
        """Return the duration for `song` (1-based) of the given SID, or
        None if the SID isn't in the DB or the subtune index is unknown."""
        try:
            digest = md5_of_sid(sid_bytes)
        except ValueError:
            return None
        durs = self.entries.get(digest)
        if durs is None:
            return None
        idx = song - 1
        if idx < 0 or idx >= len(durs):
            return None
        return durs[idx]


def song_length(sid_bytes: bytes, song: int,
                lengths_path: str | None = None) -> float | None:
    """Convenience: parse `lengths_path` once-per-call and look up.

    For long-lived processes that look up many SIDs, instantiate
    ``LengthsDB`` once and call ``.lookup()`` instead — this helper
    re-reads the file every call."""
    if lengths_path is None or not os.path.exists(lengths_path):
        return None
    try:
        db = LengthsDB.load(lengths_path)
    except Exception:
        log.exception("songlengths: load failed")
        return None
    return db.lookup(sid_bytes, song)
