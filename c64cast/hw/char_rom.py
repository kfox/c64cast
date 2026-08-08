"""Character ROM: one resolver, one cache location, and a dump off the
machine in front of you.

Every C64-native glyph c64cast draws — the bitmap-mode text overlays
(`scrolling_text` → `TextSurface` → `bitmap_text.load_glyphs`), `big_text`'s
8×-scaled scroller, the on-C64 menu, the oscilloscope's text rows, and the
preview/recording renderers — reads its 8×8 cells straight out of the C64
character ROM. Without one, `framebuffer._builtin_charset()` synthesises an
ASCII font with `cv2.putText`: it renders *something*, but it is not the C64
font and PETSCII graphics codes come out blank. A user report of "the
scrolling text looks bad" was exactly this, and nothing else.

The three loaders that wanted a charset used to each carry their own
cwd-relative default (`assets/roms/characters.901225-01.bin`), which resolves
only when the process happens to be running from a source checkout with a ROM
already dropped in it. This module is the single answer instead:

  * **resolve** — an explicit configured path, else the data dir, else the
    legacy cwd-relative checkout path (so an existing checkout keeps working),
    else None → the cv2 fallback.
  * **dump** — the ROM is not RAM (`read_memory($D000)` sees I/O), so getting
    it takes a 6502 stub that banks CHAREN out and copies the ROM down into
    plain RAM the host can read back. See `api.CHAR_ROM_DUMP_STUB` and
    `C64Backend.dump_char_rom`.
  * **verify** — prove we got a charset and not I/O registers or blank RAM,
    without assuming a specific national ROM (see :func:`verify`).
  * **install** — write it, verified, under `paths.roms_dir()`.

The bytes move from the user's hardware to the user's disk and stop: nothing
here is shipped, and c64cast never redistributes a ROM.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from c64cast import paths
from c64cast.transport import atomic_write_bytes

from .c64 import SCREEN

if TYPE_CHECKING:
    from c64cast.config import Config

    from .backend import C64Backend

log = logging.getLogger(__name__)

# One 8×8 glyph is 8 bytes; one charset is 256 glyphs = 2 KB. The physical
# CHARGEN ROM holds two of them (uppercase/graphics then lowercase/uppercase);
# c64cast draws from the uppercase set, so consumers take the first 2 KB
# whichever size was installed.
GLYPH_BYTES = 8
GLYPHS_PER_SET = 256
CHARSET_BYTES = GLYPH_BYTES * GLYPHS_PER_SET  # 2048
CHARGEN_BYTES = 2 * CHARSET_BYTES  # 4096 — the full ROM

CHARGEN_FILENAME = "chargen.bin"

# Where a source checkout used to keep its dump. Kept in the resolver chain so
# a checkout that already has one doesn't suddenly lose its glyphs; it is not
# where anything gets written.
LEGACY_CHARGEN_PATH = "assets/roms/characters.901225-01.bin"

# SHA-256 of the stock Commodore 901225-01 CHARGEN — the full 4 KB ROM and its
# 2 KB uppercase half (a charset extracted from an emulator may be either).
# Purely informational: a Swedish/Danish machine, a JiffyDOS charset or a
# replacement font is exactly the charset that user wants us to use, so an
# unrecognized digest is a note, never a failure.
STOCK_DIGESTS = {
    "fd0d53b8480e86163ac98998976c72cc58d5dd8eb824ed7b829774e74213b420": "901225-01 (stock, 4 KB)",
    "3cf89732b10b1d51a267f74df35f10a154108b444a3a0ec9e51ef7ddefb668a1": (
        "901225-01 (stock, 2 KB uppercase half)"
    ),
}

# The structural check's tolerance, in mismatching bytes per 2 KB set.
#
# Screen codes $80-$FF are the reverse-video twins of $00-$7F, so the second
# half of a set is the bitwise complement of the first — a property random
# RAM or a page of I/O registers cannot fake. It is *almost* exact: the stock
# 901225-01 has exactly one byte that isn't (screen code $80, the reversed
# `@`, row 5 reads $99 where the complement is $9D), and there is no reason to
# assume a national variant has zero such quirks either. So allow a handful of
# bytes out of 1024 — garbage still misses on ~99.6% of them.
REVERSE_HALF_TOLERANCE = 8

# Screen code of a glyph that must be blank in any charset, and one that must
# not be. `SCREEN.SC_SPACE` covers the first; `A` is the second because every
# charset worth using has one, including national variants that rearrange the
# accented letters around it.
SC_LETTER_A = 0x01


@dataclass(frozen=True)
class VerifyResult:
    """Verdict on a candidate charset. `ok` gates writing it to disk; `note`
    carries the (never fatal) digest identification for logs and `--doctor`."""

    ok: bool
    size: int
    sha256: str
    note: str
    error: str | None = None

    def describe(self) -> str:
        """One line for a human: the verdict plus the digest identification."""
        head = self.error if self.error else f"{self.size} bytes"
        return f"{head} — {self.note}"


def _glyph(data: bytes, screen_code: int) -> bytes:
    """The 8 bytes of one glyph's bitmap."""
    return data[screen_code * GLYPH_BYTES : (screen_code + 1) * GLYPH_BYTES]


def _reverse_half_misses(charset: bytes) -> int:
    """Count the bytes in one 2 KB set where the reverse-video half fails to
    complement the normal half. See :data:`REVERSE_HALF_TOLERANCE`."""
    half = CHARSET_BYTES // 2
    normal, reversed_ = charset[:half], charset[half:CHARSET_BYTES]
    return sum(1 for a, b in zip(normal, reversed_, strict=True) if a ^ b != 0xFF)


def verify(data: bytes) -> VerifyResult:
    """Check that `data` really is a C64 charset.

    The point is to catch a *botched dump* — a `$01` bank that never took, so
    the copy read I/O registers; a read that returned blank RAM; a truncated
    transfer — without rejecting a legitimately different ROM. So the checks
    are structural, not an equality test against a known image:

      * at least one full 2 KB set (accept 2 KB or 4 KB);
      * within each set, screen codes $80-$FF complement $00-$7F (see
        :data:`REVERSE_HALF_TOLERANCE`) — the check I/O and RAM cannot pass;
      * screen code $20 (space) is entirely blank and $01 (`A`) is not — a
        buffer of all $00 or all $FF satisfies the complement test trivially,
        this is what rules it out.

    The SHA-256 comparison against the stock ROM is reported in `note` and
    never affects `ok`.
    """
    size = len(data)
    digest = hashlib.sha256(data).hexdigest()
    note = STOCK_DIGESTS.get(digest, "unrecognized variant — a national or replacement charset?")

    def bad(msg: str) -> VerifyResult:
        return VerifyResult(ok=False, size=size, sha256=digest, note=note, error=msg)

    if size < CHARSET_BYTES:
        return bad(f"too short: {size} bytes, need at least {CHARSET_BYTES}")

    for s in range(min(size // CHARSET_BYTES, 2)):
        charset = data[s * CHARSET_BYTES : (s + 1) * CHARSET_BYTES]
        misses = _reverse_half_misses(charset)
        if misses > REVERSE_HALF_TOLERANCE:
            return bad(
                f"charset {s}: reverse-video half doesn't complement the normal "
                f"half ({misses}/{CHARSET_BYTES // 2} bytes differ) — this looks "
                "like I/O or RAM, not character ROM"
            )

    if any(_glyph(data, SCREEN.SC_SPACE)):
        return bad("screen code $20 (space) is not blank — not a character ROM")
    if not any(_glyph(data, SC_LETTER_A)):
        return bad("screen code $01 (A) is blank — the dump read empty memory")

    return VerifyResult(ok=True, size=size, sha256=digest, note=note)


def installed_path() -> Path:
    """Where a dumped/installed character ROM lives
    (``<data root>/roms/chargen.bin``). May not exist."""
    return paths.roms_dir() / CHARGEN_FILENAME


def resolve(configured: str | None = None) -> Path | None:
    """The character ROM this run should use, or None for "nothing installed".

    Precedence: an explicitly configured path (``[preview] charset_path``, an
    overlay's ``charset_path``) → the data dir → the legacy cwd-relative
    checkout path → None. A configured path that doesn't exist falls through
    rather than raising: a missing charset degrades to the cv2 fallback (with
    a warning from the caller), it never kills a run.
    """
    if configured:
        p = Path(paths.expand_user(configured))
        if p.is_file():
            return p
    installed = installed_path()
    if installed.is_file():
        return installed
    legacy = Path(LEGACY_CHARGEN_PATH)
    if legacy.is_file():
        return legacy
    return None


_GLYPHS_CACHE: bytes | None = None


def invalidate_cache() -> None:
    """Drop the process-wide glyph cache so the next :func:`load_glyphs`
    re-resolves. Called after a successful dump: the run that triggered it has
    very likely already primed the cache with the cv2 fallback, and should get
    the real glyphs without a restart."""
    global _GLYPHS_CACHE
    _GLYPHS_CACHE = None


def _read_glyphs(configured: str | None) -> bytes:
    path = resolve(configured)
    if path is not None:
        try:
            data = path.read_bytes()[:CHARSET_BYTES]
        except OSError as e:
            log.warning("char_rom: could not read %s (%s); using the builtin charset", path, e)
        else:
            if len(data) == CHARSET_BYTES:
                return data
            # Zero-padding a truncated file to 2 KB would render ~1900 blank
            # cells, which looks like a render bug rather than a bad file.
            log.warning("char_rom: %s is shorter than 2 KB; using the builtin charset", path)
    # Deferred: framebuffer imports this module for its own glyphs, so a
    # top-level import here is a cycle.
    from c64cast.video.framebuffer import _builtin_charset

    return _builtin_charset()


def load_glyphs(configured: str | None = None) -> bytes:
    """The 2 KB uppercase charset for `configured` (None = resolve
    automatically). Always exactly :data:`CHARSET_BYTES` long, so callers can
    reshape it to (256, 8) without checking.

    Falls back to `framebuffer._builtin_charset()` (a cv2-rendered ASCII font)
    when nothing resolves — text still renders, it just isn't the C64 font.

    Only the automatic answer is cached process-wide: that is the hot path
    (`bitmap_text.load_glyphs`, called from every text painter), while an
    explicit path comes from a per-object constructor and is read fresh. The
    cache is deliberately not keyed by path — one shared charset is the whole
    point — so serving an explicitly-requested file out of it would hand back
    whichever one was asked for first.
    """
    if configured:
        return _read_glyphs(configured)
    global _GLYPHS_CACHE
    if _GLYPHS_CACHE is None:
        _GLYPHS_CACHE = _read_glyphs(None)
    return _GLYPHS_CACHE


def install_data(data: bytes) -> Path:
    """Verify `data` and write it to :func:`installed_path`, atomically.

    Raises ValueError with the verifier's reason when it doesn't look like a
    charset — an unverified file is never written, because a bad one is worse
    than none (the cv2 fallback at least renders legibly, garbage glyphs do
    not) and it would suppress the auto-dump forever."""
    result = verify(data)
    if not result.ok:
        raise ValueError(f"not a usable C64 character ROM: {result.error}")
    dest = installed_path()
    atomic_write_bytes(dest, data)
    invalidate_cache()
    log.debug("char_rom: installed %d bytes at %s (%s)", len(data), dest, result.note)
    return dest


def install(src: str | os.PathLike[str]) -> Path:
    """Verify the file at `src` and install it (see :func:`install_data`).
    Raises OSError if it can't be read, ValueError if it isn't a charset."""
    return install_data(Path(paths.expand_user(os.fspath(src))).read_bytes())


def dump(be: C64Backend) -> bytes:
    """Read the character ROM off the connected machine and return the raw
    4 KB, verified. Raises RuntimeError if the read failed or didn't verify,
    and :class:`~c64cast.hw.backend.BackendCapabilityError` if the backend can't
    run the stub at all."""
    data = be.dump_char_rom()
    result = verify(data)
    if not result.ok:
        raise RuntimeError(f"the character-ROM dump did not verify: {result.error}")
    log.debug("char_rom: dumped %d bytes (%s)", len(data), result.note)
    return data


def ensure_installed(be: C64Backend, cfg: Config) -> bool:
    """First-run hook: if no character ROM resolves, dump one off the machine
    that is already connected and cache it. Returns True if it dumped.

    Deliberately best-effort and never fatal — a machine that won't give up
    its ROM should still cast, with the cv2 fallback it would have had anyway.
    A failure logs the `--install-char-rom` escape route once and moves on.

    Called from `cli.build_stack` right after the reset + BASIC clear loop:
    the machine is idle, nothing has painted yet, and the Ultimate's kick
    (which soft-resets) can re-establish the clear loop behind us."""
    if not cfg.hardware.dump_char_rom:
        return False
    if resolve(cfg.preview.charset_path) is not None:
        return False
    if not (be.profile.supports_read and be.profile.supports_run_prg):
        log.debug("char_rom: backend cannot dump (read/run_prg unsupported); keeping the fallback")
        return False

    try:
        dest = install_data(dump(be))
    except Exception as e:
        log.warning(
            "char_rom: could not dump the character ROM from the C64 (%s). Text "
            "will render in the built-in ASCII font instead of the C64 one. "
            "Retry with `c64cast --dump-char-rom`, install an existing dump with "
            "`c64cast --install-char-rom PATH`, or set [hardware].dump_char_rom "
            "= false to stop trying.",
            e,
        )
        return False
    log.info("char_rom: dumped the character ROM from the C64 → %s (first run only)", dest)
    return True
