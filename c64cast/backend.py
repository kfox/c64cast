"""Hardware abstraction layer for the C64 targets c64cast can drive.

c64cast started life talking to exactly one device — an Ultimate 64 over
its split socket-DMA/REST transport — and every consumer (scenes, modes,
overlays, the playlist, the audio streamer) was duck-typed on the
[Ultimate64API](api.py) method surface, injected from a single construction
site. This module turns that implicit contract into an explicit one so a
second hardware family (the TeensyROM+) can drop in at the same seam:

  * **`C64Backend`** — the ABC every backend implements. The *write path*
    (`write_memory*`, `write_regs`, `write_region`, `flush`, plus the
    host-side cache/listener/stats bookkeeping) is **mandatory**: it carries
    100% of rendering and audio programming, so any backend that exists at
    all must provide it. Everything that needs a *response* from the machine
    (`read_memory`) or a firmware *runner* (`reset`, `run_*`) or the REU is
    **capability-gated**: the ABC ships default implementations that raise
    `BackendCapabilityError`, and callers are expected to check the matching
    `profile.supports_*` flag first.

  * **`HardwareProfile`** — a declarative description of what a given device
    *can* do (capability flags) and its operating limits (frame-rate cap,
    write-rate ceiling, the C64 memory map it assumes). Carried on every
    backend as `backend.profile`, so a scene asks "can this device read
    memory / run a PRG / stage through the REU?" instead of branching on
    `system == "NTSC"` or assuming the Ultimate DMA Service.

  * **`make_backend(cfg)`** — the factory the CLI/doctor call instead of
    constructing a concrete backend directly. Selects the family from
    `[hardware].backend`; defaults to the Ultimate backend so every existing
    config keeps working byte-for-byte.

The REU surface is deliberately NOT part of the mandatory contract: the
Ultimate exposes it as an optional sub-transport (`reu_write`, forwarded to
`socket_dma.reuwrite`) gated by `profile.supports_reu`. A backend without an
REU leaves the default raising implementation in place; the experimental
`use_reu_*` config paths must check `supports_reu` before reaching it.
"""

from __future__ import annotations

import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import numpy as np

from .c64 import KERNAL, SCREEN, SID, VECTORS, VIC

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

# Callback signature for write listeners (preview / recording / framebuffer
# shadowing). Canonical definition lives here so every backend shares it;
# api.py re-exports it for backwards compatibility.
WriteListener = Callable[[int, bytes], None]

# write_region delta strategy (shared by every backend):
# - Small dirty range (< full_threshold of buffer): one write of that slice.
# - Mid-size: chunk the dirty range into DELTA_CHUNK_BYTES slabs and diff
#   each independently, so a sparse waveform/spectrum frame doesn't degrade
#   to a full-buffer push.
# - Whole-buffer write: one write.
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

    # ---- capability flags ----------------------------------------------
    supports_write: bool = True  # the mandatory write path (always True
    #   for a usable backend; here for symmetry)
    supports_read: bool = True  # read_memory (device round-trip read)
    supports_reset: bool = True  # hard machine reset
    supports_probe: bool = True  # a cheap liveness probe
    supports_run_prg: bool = True  # launch a PRG (clear-loop, SID player)
    supports_run_crt: bool = True  # launch a CRT (cartridge)
    supports_reu: bool = True  # REU writes (use_reu_pump / use_reu_staged)
    supports_config: bool = False  # writable/readable device config API (Ultimate REST)
    supports_sampler: bool = False  # "Ultimate Audio" FPGA PCM sampler ($DF20)
    reu_bus_clean: bool = False  # REU writes don't perturb the C64 bus/SID
    writes_are_acked: bool = False  # each write returns an ack (=> flush ~free)
    kernal_irq_intact: bool = True  # the kernal IRQ chain runs at bring-up

    # ---- transport -----------------------------------------------------
    write_transport: str = "socket_dma"  # "socket_dma" | "tr_serial" | "tr_tcp"

    # ---- timing / throughput limits ------------------------------------
    default_fps: float = 60.0  # resolved system rate (NTSC/PAL)
    max_fps: float | None = None  # per-variant cap on top of default_fps
    max_write_rate_hz: float | None = None  # sustained write ceiling (pacing)

    # ---- C64 memory map assumptions ------------------------------------
    audio_ring_addr: int = 0x4000  # base of the audio DAC ring buffer


# The Ultimate family (Ultimate 64, Ultimate II+). The two are protocol-
# equivalent for c64cast's purposes, so they share one profile for now;
# a per-variant `[hardware].variant` selector + distinct profiles (e.g.
# differing `max_fps`) can be added without touching the factory contract.
# `default_fps` is a placeholder here — make_backend() overrides it from the
# configured NTSC/PAL video system, which is orthogonal to the variant.
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
    supports_sampler=True,  # "Ultimate Audio" FPGA PCM sampler (gated by probe)
    reu_bus_clean=True,  # U64 REUWRITE is an ARM-side memcpy; no bus halt
    writes_are_acked=False,  # socket DMAWRITE is fire-and-forget
    kernal_irq_intact=True,
    write_transport="socket_dma",
    max_fps=None,  # no extra cap beyond the system rate
    max_write_rate_hz=200.0,  # ~200 writes/sec DMA ceiling (see caveats)
    audio_ring_addr=0x4000,
)

# TeensyROM+ over the token protocol (USB serial or raw TCP). Confirmed
# capabilities from the firmware source: reset (0x64EE), run_prg-equivalent
# (PostFile 0x64BB upload + LaunchFile 0x6444), ping/fw-check probe, and (in
# cycle-clean fw v0.7.2.5+) read-C64-memory (ReadC64Mem 0x64FD), which gates
# the keyboard poller + launcher idle-detect. `supports_read` is declared True
# at the protocol level here, but TeensyROMBackend.__init__ *probes* for the
# token at connect and downgrades it for older firmware that lacks it. No
# REUWRITE, so the experimental REU-staged paths fall back to the standard
# write paths. `default_fps` is overridden from the configured NTSC/PAL system
# by make_backend; the `write_transport` is set per the chosen transport.
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
    supports_sampler=False,  # no FPGA PCM sampler (Ultimate-only feature)
    reu_bus_clean=False,
    writes_are_acked=True,  # every write returns Ack/Fail -> flush ~free
    kernal_irq_intact=True,
    write_transport="tr_serial",
    max_fps=None,
    max_write_rate_hz=None,  # to be measured on hardware
    audio_ring_addr=0x4000,
)

# Registry of selectable backend families. Maps the `[hardware].backend`
# token to its base profile.
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

    # ==================================================================
    # MANDATORY — the write path + sync barrier + lifecycle + host-side
    # bookkeeping. Carries all rendering and audio programming.
    # ==================================================================
    @abstractmethod
    def write_memory(self, address: str, data_hex: str) -> None: ...

    @abstractmethod
    def write_memory_file(self, address: str, data_bytes: bytes) -> None: ...

    @abstractmethod
    def write_regs(self, base_addr: str, *values: int) -> None: ...

    @abstractmethod
    def write_region(
        self, address: int, data: bytes, region_id: int | None = None, full_threshold: float = 0.6
    ) -> int: ...

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

    # ==================================================================
    # CAPABILITY-GATED — default impls raise. Override + flip the matching
    # profile flag to support. Callers gate on profile.supports_* first.
    # ==================================================================
    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        raise BackendCapabilityError("read_memory")

    def reset(self) -> None:
        raise BackendCapabilityError("reset")

    def probe(self, timeout: float = 2.0) -> str | None:
        # Soft default: a backend with no liveness probe simply reports
        # "unknown" rather than erroring (the write transport's own connect
        # already validated reachability).
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
    ) -> None:
        """Load + start a SID tune on the real 6510. `defer_audio=True` loads the
        player but leaves it silent until `begin_sid_audio()` — used by
        WaveformScene so the oscilloscope is on screen before the first note (on
        backends that can defer; others start immediately and ignore the flag)."""
        raise BackendCapabilityError("run_sid_player")

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

    def reu_write(self, reu_offset: int, data: bytes) -> None:
        raise BackendCapabilityError("reu_write")

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
        ``profile.supports_config`` first (AsidScene reads the SID socket
        detection + snapshots the SID address map to restore on teardown)."""
        raise BackendCapabilityError("get_config_category")

    # ---- semantic write helpers ---------------------------------------
    # Pure writes presuming the standard C64 memory map + kernal IRQ chain.
    # Default impls raise here on the ABC; BufferedWriteBackend (which every
    # real backend extends) implements them via plain writes, so any
    # write-capable backend gets them for free.
    def silence_sid(self) -> None:
        raise BackendCapabilityError("silence_sid")

    def blank_display(self) -> None:
        raise BackendCapabilityError("blank_display")

    def restore_kernal_irq_vector(self) -> None:
        raise BackendCapabilityError("restore_kernal_irq_vector")

    def suppress_cursor_blink(self) -> None:
        raise BackendCapabilityError("suppress_cursor_blink")

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
        # Per region: (raw_bytes, uint8_view). The view is cached alongside
        # the bytes so write_region's diff doesn't re-wrap a fresh
        # np.frombuffer on every call. Bytes are immutable so the view stays
        # valid for the lifetime of the cache entry.
        self._cache: dict[int, tuple[bytes, np.ndarray]] = {}
        self._stats: dict[str, int] = {
            "writes": 0,
            "skipped": 0,
            "errors": 0,
            "bytes": 0,
        }
        # Write listeners (preview, recording, framebuffer shadowing). Each
        # callback receives (address: int, data: bytes) for every write that
        # reaches the wire. Synchronous — listeners must not block.
        self._listeners: list[WriteListener] = []
        # Consecutive transport-write failures, driven by the shared _emit
        # error ladder (_note_emit_success / _note_emit_failure). Reset to 0
        # on any success so the ladder only escalates on a sustained outage.
        self._consecutive_errors = 0

    # ---- transport primitive (subclass implements) ------------------------
    # Labels for the shared _emit failure-log ladder. Subclasses override so
    # their log lines name the right transport (e.g. "U64 dma write" / "U64",
    # "TR write" / "TR").
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

    # ---- listeners --------------------------------------------------------
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
                log.exception("write listener raised; continuing")

    # ---- write path -------------------------------------------------------
    def write_memory(self, address: str, data_hex: str) -> None:
        """Short hex write."""
        addr = int(address, 16)
        payload = bytes.fromhex(data_hex)
        self._emit(addr, payload)
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

    def write_region(
        self, address: int, data: bytes, region_id: int | None = None, full_threshold: float = 0.6
    ) -> int:
        """Push data, but only the changed sub-range if we have a cached copy.

        Returns bytes actually uploaded (0 = unchanged, skipped).

        Strategy:
          * No prior cache OR length mismatch → full upload.
          * Contiguous dirty span < full_threshold of buffer → upload the span.
          * Else → chunked diff: split the buffer into DELTA_CHUNK_BYTES
            slabs, upload only the slabs that changed. This keeps sparse
            updates (waveform traces, spectrum bars) from degrading to a
            full push when the dirty range happens to span the whole region
            but only a fraction of cells actually differ.
        """
        key = region_id if region_id is not None else address
        # Skip the defensive bytes() copy when the caller already gave us
        # bytes (the common case — .tobytes() on a numpy array). bytes(b"...")
        # in CPython returns the same object, but bytes(bytearray) copies,
        # so the isinstance guard saves the copy only when it'd be wasted.
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

        # argmax on a bool array returns the index of the first True; doing
        # the same on the reversed view gives distance-from-end. Two linear
        # scans, but neither allocates the (variable-length) index array that
        # np.where would. The chunked branch below builds that array lazily
        # only when it's actually needed.
        first = int(np.argmax(diff))
        last = len(diff) - int(np.argmax(diff[::-1]))
        span = last - first

        if span / len(new) < full_threshold:
            self.write_memory_file(f"{address + first:04X}", new[first:last])
            self._cache[key] = (new, arr_new)
            return span

        # Wide dirty range. Try chunked diff before giving up to a full push.
        # The threshold here picks "did chunking save enough bytes to be
        # worth the extra requests?" — if the chunked uploads would total
        # more than `full_threshold` of the buffer, skip the overhead and
        # do one big push.
        n = len(new)
        if n >= DELTA_CHUNK_BYTES * 2:
            # Mark each chunk as dirty if any byte within it differs.
            n_chunks = (n + DELTA_CHUNK_BYTES - 1) // DELTA_CHUNK_BYTES
            chunk_dirty = np.zeros(n_chunks, dtype=bool)
            idx = np.flatnonzero(diff)
            chunk_dirty[idx // DELTA_CHUNK_BYTES] = True
            dirty_total = int(chunk_dirty.sum()) * DELTA_CHUNK_BYTES
            if dirty_total < n * full_threshold:
                uploaded = 0
                for ci in np.flatnonzero(chunk_dirty):
                    start = int(ci) * DELTA_CHUNK_BYTES
                    end = min(start + DELTA_CHUNK_BYTES, n)
                    self.write_memory_file(f"{address + start:04X}", new[start:end])
                    uploaded += end - start
                self._cache[key] = (new, arr_new)
                return uploaded

        # Everything else: just push the whole region in one write.
        self.write_memory_file(f"{address:04X}", new)
        self._cache[key] = (new, arr_new)
        return n

    # ---- host-side bookkeeping -------------------------------------------
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

    # ---- semantic write helpers (pure writes; shared by all backends) -----
    def silence_sid(self) -> None:
        """Mute SID output without resetting the machine. Writes 0 to $D418
        (master volume) and 0 to each voice's gate so envelopes release."""
        # Volume + filter-mode register: low nibble is volume.
        self.write_memory(f"{SID.MODE_VOL:04X}", "00")
        # Clear the gate bit on each voice so the envelope generator releases.
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

    def suppress_cursor_blink(self) -> None:
        """Write $80 to BLNSW ($00CC) so the kernal editor's cursor-blink
        code (run from the $EA31 IRQ tail) skips its toggle. Needed after
        teardown of anything that left the 6510 parked outside BASIC's
        GOTO 20 loop — e.g. our SID player MC's `JMP *` spin survives
        scene teardown, so BASIC isn't running its tight loop anymore and
        the editor IRQ path may visibly toggle one screen cell at the
        kernal-tracked cursor position. The next scene's screen-RAM paint
        overwrites the frozen cursor cell. Verified live 2026-05-26: a
        single DMA write of $80 sticks (editor doesn't actively clobber)
        and visibly stops the blink on real U64 hardware."""
        self.write_memory(f"{SCREEN.BLNSW:04X}", "80")


def make_backend(cfg: Config) -> C64Backend:
    """Construct the hardware backend selected by ``[hardware].backend``.

    Defaults to the Ultimate backend, so a config with no ``[hardware]``
    section behaves exactly as before. Connection failures (e.g. the DMA
    service being disabled) propagate from the concrete backend's
    constructor — the caller surfaces a user-actionable message.

    Raises ``ValueError`` for an unknown backend token.
    """
    backend = cfg.hardware.backend
    # NTSC/PAL is orthogonal to the hardware variant; fold the resolved
    # system rate into the profile here so the playlist reads one number.
    fps = 60.0 if cfg.ultimate64.system == "NTSC" else 50.0

    if backend == "ultimate":
        from .api import Ultimate64API

        profile = replace(ULTIMATE_PROFILE, default_fps=fps)
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
                # No explicit device — try to find the TR's USB-serial node
                # by its USB (VID, PID) across macOS/Linux/Windows.
                port = autodetect_serial_port()
                if port:
                    log.info("[teensyrom] auto-detected serial device %s", port)
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
        profile = replace(TEENSYROM_PROFILE, default_fps=fps, write_transport=transport_kind)
        return TeensyROMBackend(transport, profile=profile, storage=tr.storage)

    raise ValueError(
        f"unknown [hardware].backend {backend!r} — known backends: {', '.join(BACKENDS)}"
    )
