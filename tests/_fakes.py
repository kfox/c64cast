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

import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from unittest import mock

from c64cast.hw.backend import HardwareProfile
from c64cast.hw.c64 import actual_rate_for_latch, kernal_cia1_latch


@contextlib.contextmanager
def quiet_logging() -> Iterator[None]:
    """Swallow log records for the duration of the block, and undo any
    root-logger reconfiguration the code under test performs.

    Use this only where the log line is incidental to what the test asserts.
    Where the message *is* the documented behavior, `assertLogs` says so and
    silences it at the same time — prefer that. (`logging.disable` outranks
    `assertLogs`, so the two must not nest.)

    Restoring the root logger is the half that matters beyond the test's own
    output: `cli.main()` calls `configure_logging`, which clears the root
    handlers and installs its own. That handler outlives the test, so every
    later INFO record in the same worker process — from modules with no
    connection to the CLI — prints to the console mid-run.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    previous = root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)
        root.handlers[:] = handlers
        root.setLevel(level)


class MachineSettingsIsolation:
    """Point **both** of ``paths.py``'s environment overrides into a private
    temporary directory for the lifetime of a test module, so tests that
    assert config **defaults** / the ``load(dumps(cfg)) == cfg`` round-trip
    are hermetic against a real ``~/.config/c64cast/settings.toml`` on the
    developer's machine (the machine-settings layer is applied inside
    ``config.load``) — and so nothing a test reaches can read or write the
    real ``~/.local/share/c64cast/`` (DAC calibrations, WLED + loop presets).
    Use from a module's ``setUpModule``/``tearDownModule``:

        _iso = MachineSettingsIsolation()
        def setUpModule():
            _iso.start()
        def tearDownModule():
            _iso.stop()

    ``$C64CAST_SETTINGS`` points at a path that does not exist (the settings
    file is *read*, and "absent" is the state a defaults test wants).
    ``$C64CAST_DATA_DIR`` points at a real, empty directory (the data dir is
    *written*, and its writers create it). It covered only the first when it
    was added, which the name did not say.
    """

    def __init__(self) -> None:
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._patch: object | None = None

    def start(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        missing = os.path.join(self._tmp.name, "no-such-settings.toml")
        data_dir = os.path.join(self._tmp.name, "data")
        os.makedirs(data_dir, exist_ok=True)
        self._patch = mock.patch.dict(
            os.environ, {"C64CAST_SETTINGS": missing, "C64CAST_DATA_DIR": data_dir}
        )
        self._patch.start()  # type: ignore[attr-defined]

    def stop(self) -> None:
        if self._patch is not None:
            self._patch.stop()  # type: ignore[attr-defined]
            self._patch = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


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
        # Device config API (Ultimate REST) surface for multi-SID tests. Tests
        # opt in via `api.profile = HardwareProfile(..., supports_config=True)`
        # and seed `config_store` to model detected sockets / current values.
        self.config_puts: list[tuple[str, str, str]] = []
        self.config_store: dict[str, dict[str, str]] = {}
        # GET /v1/info surface for dac_calibration key resolution tests. None
        # (default) mirrors a backend/firmware with no /v1/info (raises).
        self.device_info: dict[str, str] | None = None
        # Hardware capability profile — mirrors the real backends' `profile`.
        # Defaults (supports_reu=True) make build_scene resolve the no-REU
        # double_buffer "auto" path OFF, so existing tests see no change; tests
        # that want the TR's no-REU behavior set `api.profile = HardwareProfile(
        # supports_reu=False)` or override the field.
        self.profile = HardwareProfile(name="Fake", family="fake")

    @classmethod
    def ultimate(cls, *, supports_config: bool = True) -> FakeAPI:
        """A FakeAPI presenting as a config-capable Ultimate — the profile
        the SID volume / panning / autoconfig and DAC-calibration paths gate
        on. The multi-SID surface flag tracks `supports_config` because this
        factory models a U64, where the two arrive together (a U2+-shaped
        fake sets `supports_sid_config=False` explicitly). Seed `config_store`
        afterward to model mixer items / sockets."""
        api = cls()
        api.profile = HardwareProfile(
            name="Fake U64",
            family="fake",
            supports_config=supports_config,
            supports_sid_config=supports_config,
            supports_system_mode=supports_config,
        )
        return api

    @classmethod
    def u2plus(cls) -> FakeAPI:
        """A FakeAPI presenting as a refined Ultimate II+: config API present,
        no multi-SID surface, emulated-stereo-SID surface granted (the state
        refine_capabilities leaves a real U2+ in). Seed
        `config_store["Audio Output Settings"]` to model the topology."""
        api = cls()
        api.profile = HardwareProfile(
            name="Fake U2+",
            family="fake",
            supports_config=True,
            supports_sid_config=False,
            supports_emusid_mixer=True,
        )
        return api

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

    def write_region(self, addr, data, region_id=None):
        b = bytes(data)
        self.regions[addr] = b
        self.ops.append(("write_region", addr, b, region_id))
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
        self,
        sid_bytes,
        song=0,
        timeout=5.0,
        *,
        avoid=None,
        play_bank=None,
        defer_audio=False,
        play_rate=None,
    ):
        self.sid_played = (bytes(sid_bytes), song)
        self.sid_played_avoid = avoid
        self.sid_played_play_rate = play_rate
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

    def put_config_item(self, category, item, value, *, timeout=3.0):
        self.config_puts.append((category, item, value))
        self.config_store.setdefault(category, {})[item] = value

    def get_config_category(self, category, *, timeout=3.0):
        # Tests seed `config_store[category] = {item: value}` to model detected
        # sockets / current addressing; default is an empty category.
        return dict(self.config_store.get(category, {}))

    def get_device_info(self, *, timeout=3.0):
        # Tests seed `device_info` (dict) to model GET /v1/info; leaving it
        # None mirrors a backend/firmware with no /v1/info (raises, like the
        # real BackendCapabilityError default).
        if self.device_info is None:
            raise RuntimeError("no device info (fake)")
        return dict(self.device_info)

    def silence_sid(self):
        self.regs["SILENCE"] = ()

    def restore_kernal_irq_vector(self):
        self.regs["RESTORE_IRQ"] = ()

    def restore_kernal_play_rate(self):
        self.regs["RESTORE_PLAY_RATE"] = ()

    def sid_vsync_play_rate_hz(self):
        # The kernal jiffy rate — ~60 Hz on both standards (see
        # c64.kernal_cia1_latch); a fake never retunes it.
        return actual_rate_for_latch(kernal_cia1_latch(self.profile.system), self.profile.system)

    def close(self):
        pass

    def flush(self, timeout=5.0):
        pass


def make_psid(
    *,
    magic: bytes = b"PSID",
    load: int = 0x1000,
    init: int = 0x1000,
    play: int = 0x1001,
    num_songs: int = 1,
    start_song: int = 1,
    second_sid_addr: int = 0,
    model: str | None = None,
    second_model: str | None = None,
    clock: str | None = None,
    speed: int = 0,
    payload: bytes | tuple[int, ...] | list[int] = (0x60, 0x60),
) -> bytes:
    """Minimal runnable PSID v2: real header fields + payload, enough for
    parse_psid_for_player + SidHostEmu to run INIT/PLAY (the defaults are an
    RTS init and play). A nonzero `second_sid_addr` ($D420-style base) makes
    it a v3 2SID header. `model` / `second_model` ("6581"/"8580") set the
    per-chip model bits in the v2+ flags field, `clock` ("PAL"/"NTSC"/
    "PAL+NTSC") the clock bits, and `speed` the per-subtune speed word (bit N
    set = subtune N+1 is CIA-timed). PSID is a real external format
    — its byte offsets live here once, so a typo'd field fails every consumer
    instead of just the one file that happened to re-type it."""
    header = bytearray(124)
    header[0:4] = magic
    header[4:6] = (2).to_bytes(2, "big")  # version
    header[6:8] = (124).to_bytes(2, "big")  # data offset (v2 header size)
    header[8:10] = load.to_bytes(2, "big")
    header[10:12] = init.to_bytes(2, "big")
    header[12:14] = play.to_bytes(2, "big")
    header[14:16] = num_songs.to_bytes(2, "big")
    header[16:18] = start_song.to_bytes(2, "big")
    header[0x12:0x16] = speed.to_bytes(4, "big")
    if second_sid_addr:
        header[4:6] = (3).to_bytes(2, "big")  # secondSIDAddress is v3+
        header[0x7A] = (second_sid_addr >> 4) & 0xFF
    # v2+ flags at $76-$77 (big-endian): sidModel1 is bits 4-5 and sidModel2
    # bits 6-7, both in the low byte $77. 1 = 6581, 2 = 8580.
    bits = {"6581": 1, "8580": 2}
    # clock is bits 2-3 of the same low byte: 1 = PAL, 2 = NTSC, 3 = both.
    clock_bits = {"PAL": 1, "NTSC": 2, "PAL+NTSC": 3}
    flags = (
        bits.get(model or "", 0) << 4
        | bits.get(second_model or "", 0) << 6
        | clock_bits.get(clock or "", 0) << 2
    )
    header[0x76:0x78] = flags.to_bytes(2, "big")
    return bytes(header) + bytes(payload)


def fake_system_stack(name: str, scenes: list | None = None):
    """A SystemStack with every non-trivial field mocked — the ensemble and
    orchestrator tests only exercise `name` (and `cfg.scenes` when given)."""
    from c64cast.app.ensemble import SystemStack

    cfg = mock.MagicMock(name=f"cfg-{name}")
    cfg.scenes = scenes or []
    return SystemStack(
        name=name,
        cfg=cfg,
        api=mock.MagicMock(name=f"api-{name}"),
        audio=None,
        source=None,
        playlist=mock.MagicMock(name=f"playlist-{name}"),
        key_poller=mock.MagicMock(name=f"keyboard-{name}"),
        framebuffer=None,
        preview_window=None,
        recorder=None,
    )


def new_streamer(**overrides):
    """A bare-bones AudioStreamer over a FakeAPI (no thread started), built
    through the real __init__ — the PR #227 post-mortem: a __new__ plus
    hand-copied-state fixture went stale every time a field was added, and
    the missing field surfaced as an AttributeError deep inside a worker
    thread rather than as a fixture error. host_dma_servo defaults off so
    worker-path tests stay open-loop (no R reads); override per test."""
    from typing import cast

    from c64cast.audio.audio import AudioStreamer
    from c64cast.hw.api import Ultimate64API

    kwargs: dict = {"sample_rate": 8000, "system": "NTSC", "host_dma_servo": False}
    kwargs.update(overrides)
    return AudioStreamer(cast(Ultimate64API, FakeAPI()), **kwargs)


def run_irq_handler(handler: bytes, *, addr: int = 0xC100, seed: dict[int, int] | None = None):
    """Execute hand-assembled IRQ-handler bytes on a bare py65 6502 until
    they chain into the kernal (JMP $EA31 full tail / JMP $EA81 lean tail).

    `seed` is an {address: byte} map applied before the run (trackers,
    counters, fake REU registers). Returns an object with `memory` (the
    sid_host_emu.TrappedRam, so `.ram` and the `.access` read/write bitmap
    are inspectable), `exit_pc` (which kernal tail was taken) and `mpu`.
    The step budget turns a mis-assembled branch displacement — which JAMs
    a real C64 — into a loud failure instead of a hang, and a handler that
    leaves its own PHA unbalanced shows up as `mpu.sp != 0xFF`."""
    from types import SimpleNamespace

    from py65.devices.mpu6502 import MPU

    from c64cast.sid.sid_host_emu import TrappedRam

    memory = TrappedRam(track_access=True)
    memory.ram[addr : addr + len(handler)] = handler
    for seed_addr, value in (seed or {}).items():
        memory.ram[seed_addr] = value
    mpu = MPU(memory=memory)
    mpu.pc = addr
    mpu.sp = 0xFF
    kernal_tails = (0xEA31, 0xEA81)
    for _ in range(5000):
        mpu.step()
        if mpu.pc in kernal_tails:
            return SimpleNamespace(memory=memory, exit_pc=mpu.pc, mpu=mpu)
    raise AssertionError(f"handler never chained to the kernal (PC=${mpu.pc:04X})")


def bare_waveform_scene(**attrs):
    """A WaveformScene that skips the SID-loading __init__ (which needs a
    real PSID file + emulator bring-up); each caller sets exactly the
    attributes its method under test reads. One builder instead of a
    re-implemented ``_scene()`` per TestCase."""
    from c64cast.sid.waveform import WaveformScene

    scene = WaveformScene.__new__(WaveformScene)
    for name, value in attrs.items():
        setattr(scene, name, value)
    return scene


class FrozenClock:
    """A stand-in for the stdlib ``time`` module with one function pinned.

    Bind it over a **module's own** ``time`` name::

        with mock.patch.object(scenes, "time", FrozenClock(10.0)):
            ...                       # scenes.time.time() == 10.0

    and never over an attribute of the stdlib module itself
    (``mock.patch.object(scenes.time, "time", return_value=10.0)``). The
    latter rebinds ``time.time`` for the entire process, so every thread in
    the suite reads the frozen value too — and the suite leaves worker
    threads running. A worker measuring an interval against a clock that
    never advances, or that jumps decades when the patch lifts, is a flake
    with no connection to the test that caused it. The same aliasing already
    broke the preview pump tests under ``make coverage``, where every module
    shares one process.

    Any attribute other than the pinned one delegates to the real module, so
    code that also calls ``time.monotonic()`` or ``time.sleep()`` while the
    fake is installed keeps working.
    """

    def __init__(self, now: float, attr: str = "time") -> None:
        self._now = float(now)
        self._attr = attr

    def advance(self, dt: float) -> None:
        """Move the pinned clock forward — lets a test drive a poller's tick
        state machine on virtual time (each tick exactly poll_interval_s
        apart) instead of racing a real thread against wall time."""
        self._now += dt

    def __getattr__(self, name: str):
        # Only reached for names not on the instance, so `_now`/`_attr` never
        # route back through here.
        if name == self._attr:
            return lambda: self._now
        return getattr(time, name)
