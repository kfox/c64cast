"""Hardware abstraction layer for the C64 targets c64cast can drive.

  * **`C64Backend`** — the ABC every backend implements. The *write path*
    (`write_memory*`, `write_regs`, `write_region`, `flush`, plus the
    host-side cache/listener/stats bookkeeping) is **mandatory**; everything
    that needs a *response* from the machine (`read_memory`), a firmware
    *runner* (`reset`, `run_*`), the REU, or the config API is
    **capability-gated**, defaulting to a raising implementation the caller
    is expected to have gated on the matching `profile.supports_*` flag.
  * **`HardwareProfile`** — what a given device can do (capability flags) and
    the limits it operates under (frame-rate cap, write-rate ceiling, the link
    cost model, the C64 memory map it assumes). Carried as `backend.profile`.
  * **`BufferedWriteBackend`** — the shared host-side write path (register
    coalescing, the per-region delta cache, listeners, stats) over a single
    per-backend transport primitive, `_emit`.
  * **`make_backend(cfg)`** — the factory the CLI/doctor call instead of
    constructing a concrete backend directly.

See docs/architecture/hardware-io.md#backendpy--the-c64backend-duck-type-hardware-profiles-and-the-shared-write-path.
"""

from __future__ import annotations

import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np

from .c64 import KERNAL, SCREEN, SID, VECTORS, VIC

if TYPE_CHECKING:
    from c64cast.app.config import Config

log = logging.getLogger(__name__)

# Callback signature for write listeners (preview / recording / framebuffer
# shadowing).
WriteListener = Callable[[int, bytes], None]

# Slab size for write_region's chunked branch: the dirty range is diffed in
# slabs this big and only the dirty ones are pushed. Whether that is worth
# doing is a per-link cost question — see HardwareProfile.write_cost_s.
DELTA_CHUNK_BYTES = 256


class BackendCapabilityError(RuntimeError):
    """Raised when code invokes a backend method the active hardware doesn't
    support (e.g. ``read_memory`` on a write-only TeensyROM+ test build).

    Callers that can degrade gracefully should check the matching
    ``backend.profile.supports_*`` flag *before* calling, and treat this
    exception as a hard programming error (a capability gate was missed)
    rather than something to catch at runtime."""

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(f"this hardware backend does not support {capability!r}")


@dataclass(frozen=True)
class HardwareProfile:
    """What a hardware backend can do and the limits it operates under.

    Capability flags gate the optional methods on `C64Backend`; the numeric
    limits let the playlist/pacing layer self-tune per device. A scene or
    config validator reads these instead of hard-coding device assumptions.

    `default_fps` is the system video rate (60 NTSC / 50 PAL) resolved at
    construction; `max_fps` is an optional *per-variant* cap applied on top
    (None = no cap). This is where differing frame-rate ceilings between
    hardware variants live — the playlist clamps the system rate to it.
    """

    name: str  # human-facing, e.g. "Ultimate 64"
    family: str  # "ultimate" | "tr"

    supports_write: bool = True  # the mandatory write path
    supports_read: bool = True  # read_memory (device round-trip read)
    supports_reset: bool = True  # hard machine reset
    supports_probe: bool = True  # a cheap liveness probe
    supports_run_prg: bool = True  # launch a PRG (clear-loop, SID player)
    supports_run_crt: bool = True  # launch a CRT (cartridge)
    supports_reu: bool = True  # REU writes (use_reu_pump / use_reu_staged)
    supports_config: bool = False  # writable/readable device config API (Ultimate REST)
    supports_sid_config: bool = False  # the U64 multi-SID config surface (SID routing,
    #   socket detection, UltiSID model curves — see SID_CONFIG_CATEGORIES).
    #   Narrower than supports_config: the Ultimate II+ has the config API but
    #   none of these categories.
    supports_emusid_mixer: bool = False  # the U2+ emulated stereo SID surface
    #   (snoop routing + Vol/Pan EmuSid mixer — see EMUSID_MIXER_CATEGORY).
    #   Granted by refine_capabilities, so False on an unprobed run.
    supports_system_mode: bool = False  # the Ultimate 64's "System Mode" enum
    #   (PAL / NTSC / PAL-60 / NTSC-50 machine timing — see
    #   SYSTEM_MODE_CATEGORY). Granted by refine_capabilities, so False on an
    #   unprobed run.
    supports_sampler: bool = False  # "Ultimate Audio" FPGA PCM sampler ($DF20)
    supports_video_stream: bool = False  # the machine's own VIC-out UDP stream
    #   (socket-DMA 0xFF20/0xFF30 — see hw/vic_stream.py). Ultimate 64 only;
    #   revoked by refine_capabilities alongside supports_system_mode.
    reu_bus_clean: bool = False  # REU writes don't perturb the C64 bus/SID
    writes_are_acked: bool = False  # each write returns an ack (=> flush ~free)
    kernal_irq_intact: bool = True  # the kernal IRQ chain runs at bring-up

    write_transport: str = "socket_dma"  # "socket_dma" | "tr_serial" | "tr_tcp"

    system: str = "NTSC"  # resolved machine timing standard ("NTSC"/"PAL").
    #   Set by make_backend from [ultimate64].system and re-folded by
    #   hw_provision.resolve_system once the live System Mode has been read.
    #   Carried
    #   here so anything holding a backend (the SID player's CIA math) can
    #   reach the machine's clock without also holding the Config.
    default_fps: float = 60.0  # resolved system rate (NTSC/PAL)
    max_fps: float | None = None  # per-variant cap on top of default_fps
    max_write_rate_hz: float | None = None  # sustained write ceiling (pacing)

    # Link cost model — see `write_cost_s`. Measured per family with
    # scripts/diags/link_cost_model.py; the defaults are the Ultimate's, so an
    # unmeasured backend inherits the conservative (count-bound) shape.
    write_cost_floor_s: float = 5.2e-3  # per-write overhead payload can't touch
    write_cost_intercept_s: float = 0.8e-3
    write_cost_per_byte_s: float = 1.85e-6

    def write_cost_s(self, nbytes: int) -> float:
        """Wall-clock seconds one write of ``nbytes`` costs the frame budget.

        Two regimes, because that is what the links measure as: a fixed
        per-write overhead that the payload cannot touch, and above the knee
        where that runs out, a marginal per-byte transfer cost.

        On the Ultimate the fixed term is ~5.2 ms and payload is *free* up to
        ~2.4 KB, so what a frame spends is writes; on the TeensyROM the fixed
        term is ~0.29 ms and cost is essentially all payload, so what a frame
        spends is bytes. That 18x difference in the fixed term is what makes
        the same delta strategy right on one backend and wrong on the other.
        """
        return max(
            self.write_cost_floor_s,
            self.write_cost_intercept_s + self.write_cost_per_byte_s * nbytes,
        )

    audio_ring_addr: int = 0x4000  # base of the audio DAC ring buffer

    # The SID model in the C64 being driven, from [hardware].host_sid_model.
    # None = unknown / opted out. `assumed` marks the NTSC=6581 / PAL=8580
    # convention rather than a user declaration, so consumers can say so.
    host_sid_model: str | None = None
    host_sid_model_assumed: bool = False
    # The machine's internal SID chips as ((address, model), ...), from
    # [hardware].host_sid_chips — a dual-SID mod (ARM2SID, SIDFX, DualSID)
    # carries a second chip host_sid_model alone can't describe. Pairs rather
    # than a dict so the profile stays hashable. Empty = undeclared, which
    # leaves host_sid_model in charge.
    host_sid_chips: tuple[tuple[int, str], ...] = ()
    host_sid_tune_match: str = "off"  # [hardware].host_sid_tune_match


# The three REST config categories that make up the U64 multi-SID surface —
# address routing, socket enables/detection, UltiSID model curves. A device
# qualifies for `supports_sid_config` only when GET /v1/configs exposes ALL of
# them; the Ultimate II+ exposes none. tests/test_backend.py pins these to the
# canonical constants in c64cast/sid/asid_sidmap.py, which can't be imported
# from here (hw must not depend on sid).
SID_CONFIG_CATEGORIES = (
    "SID Addressing",
    "SID Sockets Configuration",
    "UltiSID Configuration",
)

# The one category carrying the U2+ emulated-stereo-SID surface (snoop
# topology + Vol/Pan EmuSid mixer). Registered only by the U2/U2+/U2+L
# firmware (audio_select.cc) — the U64 registers "Audio Mixer" instead.
# tests/test_backend.py pins this to the canonical constant in
# c64cast/sid/emusid_mixer.py (hw must not import sid).
EMUSID_MIXER_CATEGORY = "Audio Output Settings"

# The category carrying the Ultimate 64's "System Mode" (PAL / NTSC / PAL-60 /
# NTSC-50 machine timing). Registered by the U64 firmware only — the Ultimate
# II+ drives a real C64 whose timing is the C64's own. What the enum's labels
# actually select is documented in c64cast/hw/hw_provision.py
# (SYSTEM_MODE_TIMING); it is not what the names suggest.
SYSTEM_MODE_CATEGORY = "U64 Specific Settings"

# The Ultimate family (Ultimate 64, Ultimate II+), protocol-equivalent for
# c64cast's purposes and sharing one profile. `default_fps` is a placeholder —
# make_backend() overrides it from the configured NTSC/PAL video system.
ULTIMATE_PROFILE = HardwareProfile(
    name="Ultimate",
    family="ultimate",
    supports_write=True,
    supports_read=True,
    supports_reset=True,
    supports_probe=True,
    supports_run_prg=True,
    supports_run_crt=True,
    supports_reu=True,
    supports_config=True,  # REST config API (/v1/configs) — live SID address map, REU, sampler
    supports_sid_config=True,  # optimistic: refine_capabilities revokes it at
    #   connect on a device without the categories (U2+), so an unprobed run
    #   (--skip-probe, probe failure) keeps the pre-flag behavior.
    supports_sampler=True,  # "Ultimate Audio" FPGA PCM sampler (gated by probe)
    supports_video_stream=True,  # optimistic like supports_sid_config above
    reu_bus_clean=True,  # U64 REUWRITE is an ARM-side memcpy; no bus halt
    writes_are_acked=False,  # socket DMAWRITE is fire-and-forget
    kernal_irq_intact=True,
    write_transport="socket_dma",
    max_fps=None,  # no extra cap beyond the system rate
    max_write_rate_hz=200.0,  # ~200 writes/sec DMA ceiling (see caveats)
    # HW-measured 2026-08-12, scripts/diags/link_cost_model.py: flat at 5.22 ms
    # from 8 B to ~2.4 KB, then 1.85 us/byte — write-count-bound.
    write_cost_floor_s=5.222e-3,
    write_cost_intercept_s=0.784e-3,
    write_cost_per_byte_s=1.8454e-6,
    audio_ring_addr=0x4000,
)

# TeensyROM+ over the token protocol (USB serial or raw TCP). `supports_read`
# is declared True at the protocol level here; TeensyROMBackend.__init__ probes
# for ReadC64Mem at connect and downgrades it on firmware that lacks it.
# `default_fps` and `write_transport` are set by make_backend.
TEENSYROM_PROFILE = HardwareProfile(
    name="TeensyROM+",
    family="tr",
    supports_write=True,
    supports_read=True,  # ReadC64Mem 0x64FD (fw v0.7.2.5+); probed at connect
    supports_reset=True,  # ResetC64Token 0x64EE
    supports_probe=True,  # Ping 0x6455 / FWCheck 0x64E0
    supports_run_prg=True,  # PostFile + LaunchFile
    supports_run_crt=True,  # RemoteLaunch handles CRT launch
    supports_reu=False,  # no REUWRITE opcode
    supports_config=False,  # no device config API (Ultimate-only REST surface)
    supports_sid_config=False,  # no config API at all, so no SID config surface
    supports_sampler=False,  # no FPGA PCM sampler (Ultimate-only feature)
    reu_bus_clean=False,
    writes_are_acked=True,  # every write returns Ack/Fail -> flush ~free
    kernal_irq_intact=True,
    write_transport="tr_serial",
    max_fps=None,
    # A measured floor, not a wall (HW-measured 2026-08-05,
    # scripts/diags/audio_fm_probe.py: 188 writes/s of 64 B with zero missed
    # slots, and 557/s with zero underruns on a later run). Not raised —
    # spending that headroom measures worse; see docs/architecture/audio.md.
    max_write_rate_hz=200.0,
    # HW-measured 2026-08-12, scripts/diags/link_cost_model.py: linear in
    # payload from 64 B up, fixed term only ~0.29 ms — byte-bound, the opposite
    # regime to the Ultimate.
    write_cost_floor_s=0.287e-3,
    write_cost_intercept_s=0.210e-3,
    write_cost_per_byte_s=1.4429e-6,
    audio_ring_addr=0x4000,
)

# The `[hardware].backend` tokens the CLI/config layer offers (`--describe`,
# schema `choices`). NOT a dispatch table — the actual dispatch is the
# `if`/`elif` chain in `make_backend` below. `test_backend_choices_match_registry`
# pins this tuple against the CLI's own choices, so a token added here without a
# matching `make_backend` branch fails a test rather than surfacing at runtime.
BACKENDS: tuple[str, ...] = ("ultimate", "teensyrom")


class C64Backend(ABC):
    """Abstract base every hardware backend implements.

    The mandatory write surface is abstract — a backend can't exist without
    it. The capability-gated methods are concrete here and raise
    `BackendCapabilityError` by default, so a backend that lacks a capability
    simply doesn't override them (and sets the matching `profile.supports_*`
    flag False).
    """

    #: Set by every concrete backend in __init__.
    profile: HardwareProfile

    @abstractmethod
    def write_memory(self, address: str, data_hex: str) -> None: ...

    @abstractmethod
    def write_memory_file(self, address: str, data_bytes: bytes) -> None: ...

    @abstractmethod
    def write_regs(self, base_addr: str, *values: int) -> None: ...

    @abstractmethod
    def write_region(self, address: int, data: bytes, region_id: int | None = None) -> int: ...

    @abstractmethod
    def flush(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def invalidate_cache(self) -> None: ...

    @abstractmethod
    def invalidate_region(self, region_id: int) -> None: ...

    @abstractmethod
    def add_write_listener(self, callback: WriteListener) -> None: ...

    @abstractmethod
    def remove_write_listener(self, callback: WriteListener) -> None: ...

    @property
    @abstractmethod
    def stats(self) -> dict[str, int]: ...

    @abstractmethod
    def format_write_latency(self) -> str | None:
        """One-line summary of recent write-transport latency for the log
        (DMA socket on the Ultimate, serial/TCP on the TR), or None when no
        samples have been recorded yet."""
        ...

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        raise BackendCapabilityError("read_memory")

    def reset(self) -> None:
        raise BackendCapabilityError("reset")

    def probe(self, timeout: float = 2.0) -> str | None:
        # A backend with no liveness probe reports "unknown" rather than
        # erroring; its write transport's connect already proved reachability.
        return None

    def run_basic_clear_loop(self, timeout: float = 5.0) -> None:
        raise BackendCapabilityError("run_prg")

    def pause_idle(self) -> None:
        """Put the machine into its *paused* idle state and return.

        The Playlist calls this on a C= pause (after tearing down the scene):
        the machine is left showing a static "paused" screen while the keyboard
        poller waits for the resume-hold. The contract that matters is that the
        **kernal keyboard scan stays alive** so $028D keeps updating — otherwise
        the C=-held-to-resume gesture can never be detected and the stream is
        stranded paused.

        Default: a hard `reset()`, which on the Ultimate lands in BASIC (READY
        banner, kernal editor IRQ scanning the keyboard — $028D live). A backend
        whose `reset()` lands somewhere that does NOT scan the keyboard (e.g.
        the TeensyROM menu) must override this to reach a $028D-live idle."""
        self.reset()

    def launch_program(self, path: str, timeout: float = 10.0) -> None:
        raise BackendCapabilityError("run_prg/run_crt")

    def run_sid_player(
        self,
        sid_bytes: bytes,
        song: int = 0,
        timeout: float = 5.0,
        *,
        avoid: bytes | bytearray | None = None,
        play_bank: int | None = None,
        defer_audio: bool = False,
        play_rate: str | float | None = None,
    ) -> None:
        """Load + start a SID tune on the real 6510. `defer_audio=True` loads the
        player but leaves it silent until `begin_sid_audio()` — used by
        WaveformScene so the oscilloscope is on screen before the first note (on
        backends that can defer; others start immediately and ignore the flag).
        `play_rate` is `[ultimate64].sid_play_rate` — what a vsync-timed tune's
        PLAY should be called at; None/"off" leaves the kernal jiffy rate."""
        raise BackendCapabilityError("run_sid_player")

    def restore_kernal_play_rate(self) -> None:
        """Undo a `run_sid_player(play_rate=...)` retune of CIA #1 Timer A at
        teardown. No-op on backends without a SID player."""
        return

    def sid_vsync_play_rate_hz(self) -> float:
        """The rate a vsync-timed tune's PLAY is actually being called at, which
        is the KERNAL's jiffy rate (~60 Hz on BOTH standards) unless
        `run_sid_player(play_rate=...)` retuned it."""
        from .c64 import actual_rate_for_latch, kernal_cia1_latch

        return actual_rate_for_latch(kernal_cia1_latch(self.profile.system), self.profile.system)

    def begin_sid_audio(self) -> None:
        """Release a SID start deferred by `run_sid_player(defer_audio=True)`.
        No-op on backends that always start audio synchronously (the Ultimate's
        `run_prg` resets VIC, so the scope must be re-asserted *after* the player
        — there's no silent-and-loaded window to release)."""
        return

    def sid_audio_start_time(self) -> float | None:
        """Wall-clock (`time.time()`) instant the real SID actually began
        playing the current tune, or None if no SID is playing. WaveformScene
        anchors its host-emu scope clock to this so the trace stays locked to the
        audio across the bitmap-setup gap (which differs per backend)."""
        return None

    def cue_song_reinit(self, song: int, *, play_bank: int | None = None) -> None:
        raise BackendCapabilityError("cue_song_reinit")

    def dump_char_rom(self, timeout: float = 10.0) -> bytes:
        """Read the C64's character ROM off the machine and return the raw 4 KB.

        Needs both `supports_read` and `supports_run_prg`: the ROM is invisible
        to a host read (`$D000` is I/O until the *C64* clears CHAREN), so this
        runs a copy stub on the 6510 and reads its landing zone back. See
        :mod:`c64cast.hw.char_rom` for what the bytes are for and
        `api.build_char_rom_dump_stub` for the stub."""
        raise BackendCapabilityError("dump_char_rom")

    def reu_write(self, reu_offset: int, data: bytes) -> None:
        raise BackendCapabilityError("reu_write")

    def open_video_stream(self) -> Any:
        """A stopped :class:`~c64cast.hw.vic_stream.VicStreamReceiver` for this
        machine's own VIC output — the caller starts and stops it.

        Gated by `supports_video_stream` (Ultimate 64 only). Returned rather
        than started so the caller owns the lifetime: the stream is megabytes a
        second and must not outlive whoever is watching it."""
        raise BackendCapabilityError("open_video_stream")

    def put_config_item(
        self, category: str, item: str, value: str, *, timeout: float = 3.0
    ) -> None:
        """Set one device config item over the firmware config API (Ultimate
        REST: ``PUT /v1/configs/<category>/<item>?value=<value>``). Default
        raises — only the Ultimate exposes a writable config surface. The only
        consumer is the REU auto-provisioner, which gates on
        ``profile.supports_reu`` (Ultimate-only) before invoking, so a backend
        without an REU never reaches this."""
        raise BackendCapabilityError("put_config_item")

    def get_config_category(self, category: str, *, timeout: float = 3.0) -> dict[str, str]:
        """Read one device config category as ``{item_name: current_value}``
        (Ultimate REST: ``GET /v1/configs/<category>``). Default raises — only
        the Ultimate exposes a readable config surface. Callers gate on
        ``profile.supports_config`` first — or on ``profile.supports_sid_config``
        when the category is part of the U64 multi-SID surface (AsidScene reads
        the SID socket detection + snapshots the SID address map to restore on
        teardown)."""
        raise BackendCapabilityError("get_config_category")

    def get_device_info(self, *, timeout: float = 3.0) -> dict[str, str]:
        """Read device identity (Ultimate REST: ``GET /v1/info`` —
        ``product``/``hostname``/``unique_id``/firmware+fpga versions).
        Default raises — only the Ultimate exposes this endpoint. Used by
        :mod:`c64cast.audio.dac_calibration_store` to key a per-unit calibration file by
        the device's stable ``unique_id`` instead of its (DHCP-mutable) host
        address."""
        raise BackendCapabilityError("get_device_info")

    def describe_device(self) -> str:
        """A human-readable identity for the connected unit — model, per-unit
        serial, firmware — for the connect-time log, or ``""`` when the backend
        can't tell. Best-effort: never raises.

        Logged instead of relying on the connection target alone, because an IP
        or serial-port path names an *endpoint*, not a unit: two devices can
        trade addresses between runs, and a bug report carrying only the address
        can't say which machine produced it (nor even, for the Ultimate family,
        whether it was a U64 or a U2+)."""
        return ""

    def refine_capabilities(self) -> None:
        """Downgrade optimistic profile capability flags against the connected
        device — the same probe-and-downgrade `TeensyROMBackend` applies to
        `supports_read` at connect, hooked here for backends whose probe can't
        run in ``__init__`` (the Ultimate's REST side isn't proven reachable
        until the CLI's startup probe succeeds). Callers invoke it only on
        that already-proven path, never under ``--skip-probe``. Best-effort:
        an override that can't read the device keeps the optimistic flags and
        never raises. Default no-op."""
        return

    # Pure writes presuming the standard C64 memory map + kernal IRQ chain.
    # BufferedWriteBackend (which every real backend extends) implements them,
    # so any write-capable backend gets them for free.
    def silence_sid(self) -> None:
        raise BackendCapabilityError("silence_sid")

    def blank_display(self) -> None:
        raise BackendCapabilityError("blank_display")

    def restore_kernal_irq_vector(self) -> None:
        raise BackendCapabilityError("restore_kernal_irq_vector")

    def disable_case_switch(self) -> None:
        raise BackendCapabilityError("disable_case_switch")


class BufferedWriteBackend(C64Backend):
    """Concrete base implementing the host-side write path shared by every
    backend: register coalescing (`write_regs`), the per-region delta cache
    (`write_region`), write listeners, and stats. Subclasses implement the
    single transport primitive `_emit(addr, payload)` — the actual bytes-on-
    the-wire — plus the capability surface and `flush`/`close`.

    This is lifted verbatim from the original Ultimate64API so both the
    Ultimate (fire-and-forget socket DMA) and TeensyROM (acked serial/TCP)
    backends share one correct implementation of the cache/diff semantics.
    """

    def __init__(self) -> None:
        # Per region: (raw_bytes, uint8_view). The view is cached alongside the
        # bytes so write_region's diff doesn't re-wrap a fresh np.frombuffer on
        # every call; bytes are immutable, so it stays valid.
        self._cache: dict[int, tuple[bytes, np.ndarray]] = {}
        self._stats: dict[str, int] = {
            "writes": 0,
            "skipped": 0,
            "errors": 0,
            "bytes": 0,
        }
        self._listeners: list[WriteListener] = []
        self._consecutive_errors = 0
        self._consecutive_listener_errors = 0

    # Labels for the shared _emit failure-log ladder. Subclasses override so
    # their log lines name the right transport (e.g. "U64 dma write" / "U64").
    _EMIT_WRITE_LABEL = "write"
    _EMIT_DEVICE_LABEL = "device"

    @abstractmethod
    def _emit(self, addr: int, payload: bytes) -> None:
        """Push `payload` to C64 address `addr` over the backend's transport.
        Implementations own success stat counting (`_stats['writes']`) and
        must not raise on transient transport failures — wrap the transport
        call in try/except and route the two outcomes through
        `_note_emit_success()` / `_note_emit_failure(addr, e)` so every
        backend shares one escalating failure ladder."""
        ...

    def _note_emit_success(self) -> None:
        """Clear the consecutive-failure counter after a successful write."""
        self._consecutive_errors = 0

    def _note_emit_failure(self, addr: int, e: Exception) -> None:
        """Record a transport write failure on the escalating log ladder
        shared by every backend's `_emit`. Counts the error, then logs at
        debug (first failure), warning (10th & 50th), and error (200th) so
        the user eventually sees a sustained outage even without -vv. Never
        raises — a transient blip shouldn't crash the playlist; the next
        write retries the reconnect."""
        self._stats["errors"] += 1
        self._consecutive_errors += 1
        if self._consecutive_errors == 1:
            log.debug("%s $%04X failed: %s", self._EMIT_WRITE_LABEL, addr, e)
        elif self._consecutive_errors in (10, 50):
            log.warning(
                "%s failures: %d consecutive (last: %s)",
                self._EMIT_WRITE_LABEL,
                self._consecutive_errors,
                e,
            )
        elif self._consecutive_errors == 200:
            log.error(
                "%s unreachable? %d consecutive write failures",
                self._EMIT_DEVICE_LABEL,
                self._consecutive_errors,
            )

    def add_write_listener(self, callback: WriteListener) -> None:
        """Register a callback `(address: int, data: bytes) -> None` that
        fires for every memory write reaching the wire. Used by the local
        framebuffer (preview + recording). Callbacks run synchronously on
        the caller's thread, so keep them fast and non-blocking."""
        self._listeners.append(callback)

    def remove_write_listener(self, callback: WriteListener) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(callback)

    def _notify(self, address: int, data: bytes) -> None:
        for cb in self._listeners:
            try:
                cb(address, data)
            except Exception:
                self._consecutive_listener_errors += 1
                # _note_emit_failure's ladder, for listeners: one that fails on
                # every write would otherwise traceback up to ~200 times a
                # second.
                if self._consecutive_listener_errors in (1, 10, 50, 200):
                    log.exception("write listener raised; continuing")
                continue
            self._consecutive_listener_errors = 0

    def write_memory(self, address: str, data_hex: str) -> None:
        """Short hex write."""
        addr = int(address, 16)
        payload = bytes.fromhex(data_hex)
        self._emit(addr, payload)
        self._stats["bytes"] += len(payload)
        if self._listeners:
            self._notify(addr, payload)

    def write_memory_file(self, address: str, data_bytes: bytes) -> None:
        """Binary blob upload."""
        addr = int(address, 16)
        payload = bytes(data_bytes)
        self._emit(addr, payload)
        self._stats["bytes"] += len(payload)
        if self._listeners:
            self._notify(addr, payload)

    def write_regs(self, base_addr: str, *values: int) -> None:
        """Coalesce N contiguous register writes into one transport command.

        Example: write_regs("d020", border, bg0, bg1, bg2) replaces four
        individual write_memory calls with one push.
        """
        self.write_memory(base_addr, "".join(f"{v & 0xFF:02X}" for v in values))

    def write_region(self, address: int, data: bytes, region_id: int | None = None) -> int:
        """Push data, but only the changed sub-range if we have a cached copy.

        Returns bytes actually uploaded (0 = unchanged, skipped).

        Caller's responsibility: `address + len(data)` must stay within the
        16-bit C64 address space (0x0000-0xFFFF). This isn't checked here —
        a violation reaches `dmawrite`'s `struct.pack("<H", addr)` as a bare
        `struct.error`, not an `OSError`, so it escapes both the transport's
        reconnect handling and `_emit`'s failure ladder.

        Strategy:
          * No prior cache OR length mismatch → full upload.
          * Otherwise the choice is between one write covering the whole dirty
            span and several writes covering only the dirty DELTA_CHUNK_BYTES
            slabs within it, decided by `profile.write_cost_s` — whichever the
            link actually charges less for.

        The span write is never worse than a full push (it is a subset of the
        same bytes and cost is monotonic in payload), so a full push is just
        the case where everything is dirty and needs no branch of its own.

        Chunking trades one big write for k small ones, which is only a win
        where bytes are what the link charges for — so it is a cost comparison
        and not a byte-count one. See
        docs/architecture/hardware-io.md#the-chunking-decision-is-per-link-because-the-two-links-are-opposites.
        """
        key = region_id if region_id is not None else address
        # bytes(b"...") returns the same object in CPython but bytes(bytearray)
        # copies, so the isinstance guard skips a copy that would be wasted.
        new = data if isinstance(data, bytes) else bytes(data)
        cached = self._cache.get(key)

        if cached is None or len(cached[0]) != len(new):
            self.write_memory_file(f"{address:04X}", new)
            arr_new = np.frombuffer(new, dtype=np.uint8)
            self._cache[key] = (new, arr_new)
            return len(new)

        prev_arr = cached[1]
        arr_new = np.frombuffer(new, dtype=np.uint8)
        diff = arr_new != prev_arr
        if not diff.any():
            self._stats["skipped"] += 1
            return 0

        # argmax on a bool array is the index of the first True; on the
        # reversed view it is the distance from the end.
        first = int(np.argmax(diff))
        last = len(diff) - int(np.argmax(diff[::-1]))
        span = last - first

        n = len(new)
        cost = self.profile.write_cost_s
        span_cost = cost(span)

        # Chunking can only pay with more than one slab to skip between the
        # dirty ends; below that the span write already is the chunked write.
        if span > DELTA_CHUNK_BYTES * 2:
            n_chunks = (n + DELTA_CHUNK_BYTES - 1) // DELTA_CHUNK_BYTES
            chunk_dirty = np.zeros(n_chunks, dtype=bool)
            idx = np.flatnonzero(diff)
            chunk_dirty[idx // DELTA_CHUNK_BYTES] = True
            dirty_ids = np.flatnonzero(chunk_dirty)
            bounds = [
                (int(ci) * DELTA_CHUNK_BYTES, min((int(ci) + 1) * DELTA_CHUNK_BYTES, n))
                for ci in dirty_ids
            ]
            if sum(cost(end - start) for start, end in bounds) < span_cost:
                uploaded = 0
                for start, end in bounds:
                    self.write_memory_file(f"{address + start:04X}", new[start:end])
                    uploaded += end - start
                self._cache[key] = (new, arr_new)
                return uploaded

        self.write_memory_file(f"{address + first:04X}", new[first:last])
        self._cache[key] = (new, arr_new)
        return span

    def invalidate_cache(self) -> None:
        """Drop the dirty-region cache. Call after anything that changes VIC
        memory layout (mode switches, bank changes, machine reset)."""
        self._cache.clear()

    def invalidate_region(self, region_id: int) -> None:
        """Drop one region's cache entry so its next `write_region` re-pushes
        in full. A post-render overlay (e.g. the on-C64 menu) that paints over
        a scene which rewrites the same addresses every frame needs this: the
        scene clobbers the overlay's cells, but the overlay's own per-region
        cache would otherwise see its content unchanged and skip the repaint —
        leaving the panel painted once and then overwritten."""
        self._cache.pop(region_id, None)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def silence_sid(self) -> None:
        """Mute SID output without resetting the machine. Writes 0 to $D418
        (master volume) and 0 to each voice's gate so envelopes release."""
        self.write_memory(f"{SID.MODE_VOL:04X}", "00")
        for v in range(SID.N_VOICES):
            self.write_memory(f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}", "00")

    def blank_display(self) -> None:
        """Turn the VIC display off (clear $D011 DEN, bit 4) so the screen
        shows a solid border color instead of whatever's in screen / bitmap
        RAM. Used right before a machine reset: during the reset-latency
        window the VIC keeps the outgoing scene's mode + VIC bank, so a hires
        / bitmap scene flashes its leftover RAM as a glitchy image until the
        kernal reinitializes VIC. Blanking first replaces that flash with a
        clean solid color. Written value is text mode with DEN cleared — the
        non-DEN bits are irrelevant while the screen is blanked."""
        blanked = 0x1B & ~VIC.D011_DISPLAY_ENABLE  # $0B: standard CR1, DEN off
        self.write_memory(f"{VIC.D011_CONTROL_1:04X}", f"{blanked:02X}")

    def disable_case_switch(self) -> None:
        """Suppress the kernal's C= + SHIFT character-set toggle.

        $0291 bit 7 = 1 tells the kernal's keyboard scan to ignore the
        C= + SHIFT chord that would otherwise flip between the uppercase/
        graphics and lowercase/uppercase charsets. We use C= as the pause
        gesture, so the user often holds it while a SHIFT happens to be
        down — without this, the displayed scene's text suddenly switches
        case mid-stream."""
        self.write_memory(f"{SCREEN.CASE_SWITCH:04X}", "80")

    def restore_kernal_irq_vector(self) -> None:
        """Point $0314/$0315 back at the kernal default ($EA31). Call this
        after teardown of anything that hooks the IRQ (SID players)."""
        self.write_regs(
            f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
        )


def resolve_host_sid_model(configured: str, system: str) -> tuple[str | None, bool]:
    """The SID model to assume in the host C64: ``[hardware].host_sid_model``,
    or the NTSC→6581 / PAL→8580 convention under ``"auto"``. Returns
    ``(model, assumed)`` — model None when opted out (``"unknown"``); assumed
    True when the convention picked it (a weak heuristic, so consumers must
    report it as an assumption, not a fact)."""
    if configured == "unknown":
        return None, False
    if configured != "auto":
        return configured, False
    return ("6581" if system.upper() == "NTSC" else "8580"), True


def resolve_host_sid_chips(configured: Mapping[str, str]) -> tuple[tuple[int, str], ...]:
    """``[hardware].host_sid_chips`` as ``((address, model), ...)``, sorted by
    address. Keys are hex (``"d420"``, ``"$D420"``); a chip declared
    ``"unknown"`` keeps its address — the machine has a chip there, we just
    can't judge its model — so the verdict reports it rather than calling the
    address unmapped. Config validation has already range-checked these
    (:func:`c64cast.app.config._validate_host_sid_chips`)."""
    return tuple(
        sorted((int(str(address).lstrip("$"), 16), model) for address, model in configured.items())
    )


def make_backend(cfg: Config) -> C64Backend:
    """Construct the hardware backend selected by ``[hardware].backend``.

    Defaults to the Ultimate backend, so a config with no ``[hardware]``
    section behaves exactly as before. Connection failures (e.g. the DMA
    service being disabled) propagate from the concrete backend's
    constructor — the caller surfaces a user-actionable message.

    Raises ``ValueError`` for an unknown backend token.
    """
    backend = cfg.hardware.backend
    # `system = "auto"` can't be settled yet (it needs a live REST read, and
    # there is no API until this function returns) — assume NTSC and let
    # hw_provision.resolve_system re-fold these fields once the answer is in.
    # Normalize once: nothing at config load enforces SYSTEM_CHOICES' canonical
    # spelling, so `system = "ntsc"` reaches here intact and a bare
    # `== "NTSC"` would fold it onto the PAL fps with no diagnostic.
    configured_system = cfg.ultimate64.system.upper()
    system = "NTSC" if configured_system == "AUTO" else configured_system
    fps = 60.0 if system == "NTSC" else 50.0
    host_model, host_model_assumed = resolve_host_sid_model(cfg.hardware.host_sid_model, system)
    host_chips = resolve_host_sid_chips(cfg.hardware.host_sid_chips)
    if host_chips:
        # An explicit chip list describes the machine outright, so the NTSC/PAL
        # convention has nothing left to guess at.
        host_model_assumed = False

    if backend == "ultimate":
        from .api import Ultimate64API

        profile = replace(
            ULTIMATE_PROFILE,
            system=system,
            default_fps=fps,
            host_sid_model=host_model,
            host_sid_model_assumed=host_model_assumed,
            host_sid_chips=host_chips,
            host_sid_tune_match=cfg.hardware.host_sid_tune_match,
        )
        return Ultimate64API(
            cfg.ultimate64.url,
            dma_port=cfg.ultimate64.dma_port,
            dma_password=cfg.ultimate64.dma_password,
            profile=profile,
        )

    if backend == "teensyrom":
        from .teensyrom_api import TeensyROMBackend
        from .teensyrom_dma import (
            DEFAULT_BAUD,
            DEFAULT_TCP_PORT,
            SerialTransport,
            TcpTransport,
            TRTransport,
            autodetect_serial_port,
        )

        tr = cfg.teensyrom
        transport: TRTransport
        if tr.transport == "serial":
            port = tr.serial_port
            if not port:
                port = autodetect_serial_port()
                if port:
                    log.info("[teensyrom] auto-detected serial device %s", port)
                    # dac_calibration_store.resolve_calibration_key only looks
                    # up the board's USB serial number when serial_port is set;
                    # left empty, two TR+ boards on one host would collide on
                    # the generic "tr-serial-auto" calibration file.
                    tr.serial_port = port
            if not port:
                raise ValueError(
                    "[teensyrom].serial_port is required when transport = "
                    '"serial" — auto-detection found no attached TeensyROM. '
                    "Set it explicitly (e.g. /dev/cu.usbmodem* or COM3) over a "
                    "plain USB data cable — not an FTDI null-modem cable."
                )
            transport = SerialTransport(port, tr.baud or DEFAULT_BAUD)
            transport_kind = "tr_serial"
        elif tr.transport == "tcp":
            if not tr.host:
                raise ValueError(
                    '[teensyrom].host is required when transport = "tcp" '
                    '(the TR\'s IP; find it via CCGMS "ATC" or RTC sync)'
                )
            transport = TcpTransport(tr.host, tr.tcp_port or DEFAULT_TCP_PORT)
            transport_kind = "tr_tcp"
        else:
            raise ValueError(f"unknown [teensyrom].transport {tr.transport!r} (want: serial, tcp)")
        profile = replace(
            TEENSYROM_PROFILE,
            system=system,
            default_fps=fps,
            write_transport=transport_kind,
            host_sid_model=host_model,
            host_sid_model_assumed=host_model_assumed,
            host_sid_chips=host_chips,
            host_sid_tune_match=cfg.hardware.host_sid_tune_match,
        )
        return TeensyROMBackend(transport, profile=profile, storage=tr.storage)

    raise ValueError(
        f"unknown [hardware].backend {backend!r} — known backends: {', '.join(BACKENDS)}"
    )
