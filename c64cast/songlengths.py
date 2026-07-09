"""HVSC SongLengths database lookup.

The High Voltage SID Collection ships a `Songlengths.md5` file mapping
the MD5 of each SID file to a list of per-subtune durations. Format:

    ; comment lines start with `;`
    <32-char-md5>=<dur1> <dur2> <dur3> ...

Each `dur` is "M:SS" or "M:SS.mmm". Subtune 1 → durations[0], etc.

The key is a plain MD5 of the whole SID file (header + data) — see
`md5_of_sid` for why this isn't the fancier field-selective digest
libsidplayfp's `SidTune::createMD5()` computes.

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
    """Return the HVSC-style MD5 hex digest for a SID file.

    Despite what libsidplayfp's own `createMD5()` computes (a fingerprint
    over selected header fields + data, deliberately excluding the free-text
    name/author/released strings so re-tagged rips still match), the
    Songlengths.md5 shipped with the HVSC is keyed by a plain MD5 of the
    *entire raw file*, header included — verified directly against entries
    in DOCUMENTS/Songlengths.md5 (e.g. Galway's Times_of_Lore.sid)."""
    return hashlib.md5(sid_bytes).hexdigest()


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
        log.info("songlengths: loaded %d entries from %s", len(entries), path)
        return cls(entries=entries)

    def lookup(self, sid_bytes: bytes, song: int = 1) -> float | None:
        """Return the duration for `song` (1-based) of the given SID, or
        None if the SID isn't in the DB or the subtune index is unknown."""
        durs = self.entries.get(md5_of_sid(sid_bytes))
        if durs is None:
            return None
        idx = song - 1
        if idx < 0 or idx >= len(durs):
            return None
        return durs[idx]


def song_length(sid_bytes: bytes, song: int, lengths_path: str | None = None) -> float | None:
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
