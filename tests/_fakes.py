"""Shared test doubles. Import these instead of redefining per-file FakeAPIs.

The unified `FakeAPI` exposes the full write/read surface of
`Ultimate64API` (write_memory, write_memory_file, write_regs, write_region,
invalidate_cache, read_memory, close, flush) plus the waveform-specific
helpers (run_sid_player, cue_song_reinit, silence_sid, restore_kernal_irq_vector).

Snapshots: tests inspect the last-write-per-address via `regions`, `regs`,
`mem_files`, `memories`. Chronology: `writes` is a flat list of every
write_memory_file call as (addr_upper, bytes). Read injection: set
`canned_regs` to drive read_memory($D400, 25); other reads return None.
"""

from __future__ import annotations

import time

from c64cast.backend import HardwareProfile


class FakeSocketDMA:
    """Stand-in for the `socket_dma` attribute on Ultimate64API. Records
    REUWRITE calls so tests can verify REU pump preload behavior."""

    def __init__(self):
        # List of (reu_offset, bytes) tuples in call order.
        self.reuwrites: list[tuple[int, bytes]] = []

    def reuwrite(self, reu_offset: int, data: bytes) -> None:
        self.reuwrites.append((reu_offset, bytes(data)))


class FakeAPI:
    def __init__(self):
        self.regions: dict[int, bytes] = {}
        self.regs: dict[str, tuple[int, ...]] = {}
        self.mem_files: dict[str, bytes] = {}
        self.memories: dict[str, str] = {}
        self.writes: list[tuple[str, bytes]] = []
        # Unified sequential op log. Each entry = (op_name, *args). Used
        # by tests that need to assert relative ORDER across different
        # write surfaces (e.g. "stub upload happened BEFORE IRQ vector
        # hook"). `writes` / `mem_files` / `memories` / `regs` are still
        # the right things to use for last-write-wins lookups.
        self.ops: list[tuple] = []
        self.cache_invalidations = 0
        self.region_invalidations: list[int] = []
        self.sid_played: tuple[bytes, int] | None = None
        # Tracks each cue_song_reinit(song) call in order. Tests inspect
        # this to verify the SHIFT cycle path uses the fast in-place
        # re-INIT instead of going back through run_sid_player.
        self.cue_song_reinits: list[int] = []
        self.cue_song_reinit_play_banks: list[int | None] = []
        self.canned_regs: bytes = bytes(25)
        self.socket_dma = FakeSocketDMA()
        # Hardware capability profile — mirrors the real backends' `profile`.
        # Defaults (supports_reu=True) make build_scene resolve the no-REU
        # double_buffer "auto" path OFF, so existing tests see no change; tests
        # that want the TR's no-REU behavior set `api.profile = HardwareProfile(
        # supports_reu=False)` or override the field.
        self.profile = HardwareProfile(name="Fake", family="fake")

    def write_memory(self, addr, data_hex):
        self.memories[str(addr).upper()] = data_hex
        self.ops.append(("write_memory", str(addr).upper(), data_hex))

    def write_memory_file(self, addr, data):
        b = bytes(data)
        key = str(addr).upper()
        self.mem_files[key] = b
        self.writes.append((key, b))
        self.ops.append(("write_memory_file", key, b))

    def write_regs(self, base, *vals):
        self.regs[str(base).upper()] = tuple(vals)
        self.ops.append(("write_regs", str(base).upper(), tuple(vals)))

    def write_region(self, addr, data, region_id=None, full_threshold=0.6):
        b = bytes(data)
        self.regions[addr] = b
        return len(b)

    def reu_write(self, reu_offset, data):
        # Mirror Ultimate64API.reu_write, which forwards to socket_dma so
        # existing assertions on socket_dma.reuwrites keep working.
        self.socket_dma.reuwrite(reu_offset, data)

    def invalidate_cache(self):
        self.cache_invalidations += 1

    def invalidate_region(self, region_id):
        self.region_invalidations.append(region_id)

    def read_memory(self, address, length, timeout=1.0):
        if address == 0xD400 and length == 25:
            return self.canned_regs
        return None

    def run_sid_player(
        self, sid_bytes, song=0, timeout=5.0, *, avoid=None, play_bank=None, defer_audio=False
    ):
        self.sid_played = (bytes(sid_bytes), song)
        self.sid_played_avoid = avoid
        self.sid_played_play_bank = play_bank
        self.sid_deferred = defer_audio
        # Mirror the real backends: when not deferred, audio starts now; when
        # deferred, the start time is recorded at begin_sid_audio().
        if not defer_audio:
            self._sid_audio_start = time.time()

    def begin_sid_audio(self):
        self.sid_audio_began = True
        if getattr(self, "_sid_audio_start", None) is None:
            self._sid_audio_start = time.time()

    def sid_audio_start_time(self):
        return getattr(self, "_sid_audio_start", None)

    def cue_song_reinit(self, song, *, play_bank=None):
        self.cue_song_reinits.append(song)
        self.cue_song_reinit_play_banks.append(play_bank)

    def silence_sid(self):
        self.regs["SILENCE"] = ()

    def restore_kernal_irq_vector(self):
        self.regs["RESTORE_IRQ"] = ()

    def suppress_cursor_blink(self):
        self.regs["SUPPRESS_BLINK"] = ()

    def close(self):
        pass

    def flush(self, timeout=5.0):
        pass
