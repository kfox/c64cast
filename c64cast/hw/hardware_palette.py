"""`[color].hardware_palette`: pushing a scene's own 16 colors to the machine.

See docs/architecture/video-color.md#hardware_palettepy--pushing-a-scenes-own-16-colors-colorhardware_palette.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import numpy as np

from c64cast.app.config import ColorCfg, Config, scene_and_clip_cfgs, scene_color
from c64cast.hw import kawari, uci

if TYPE_CHECKING:
    from c64cast.video.palette_shots import PaletteShot

    from .api import Ultimate64API
    from .backend import C64Backend

log = logging.getLogger(__name__)

# The DMA client redials a connection the firmware dropped, but cannot resend
# the commands that connection may have discarded, so a push that went out
# on it fails once.
_PUSH_ATTEMPTS = 2


class PaletteWriter(Protocol):
    """One machine's way of changing the 16 colors it emits.

    `push` is the checked transaction: True only when the machine took the
    table. `load`, `go`, `ack` and `abort` are the split push `ShotPusher`
    drives at shot cuts; `go` must not flush, so a caller that sends it behind
    a frame's writes has the colors change on that frame. Any of the four may
    raise on a link failure."""

    #: Names the machine in log lines, as in "the Ultimate's own palette".
    device: str
    #: Bits per channel the machine shows; a fitted table is rounded to them.
    channel_bits: int
    #: How long `load` takes, which sets the shortest stretch worth a table.
    load_time_s: float
    #: How long after `go` the machine is ready for `ack`.
    ack_delay_s: float
    #: Whether `load`, `go`, `ack` and `abort` work, so a video's palette can
    #: follow its shots.
    follows_shots: bool
    #: Why `read_machine` returned None, for the warning.
    unreadable: str

    def read_machine(self) -> np.ndarray | None:
        """The 16 colors the machine shows now (BGR uint8), or None."""
        ...

    def watch_resets(self, callback: Callable[[], None]) -> None:
        """Call `callback` after each C64 reset that puts the machine's own
        palette back, so a scene's table can be pushed again."""
        ...

    def unwatch_resets(self, callback: Callable[[], None]) -> None: ...

    def push(self, table_bgr: np.ndarray) -> bool: ...

    def load(self, table_bgr: np.ndarray, stop: Callable[[], bool]) -> bool: ...

    def go(self) -> None: ...

    def ack(self) -> None: ...

    def abort(self) -> None: ...


def _to_rgb(table_bgr: np.ndarray) -> list[tuple[int, int, int]]:
    return [(int(r), int(g), int(b)) for b, g, r in table_bgr]


def _table_name(table_bgr: np.ndarray) -> str:
    digest = hashlib.sha256(np.asarray(table_bgr, dtype=np.uint8).tobytes()).hexdigest()
    return f"hw-source:{digest[:6]}"


class UltimatePaletteWriter:
    """An Ultimate 64 or C64 Ultimate, over the UCI palette commands."""

    device = "the Ultimate"
    channel_bits = 8
    load_time_s = 1.2
    ack_delay_s = uci.ACK_DELAY_S
    follows_shots = True
    unreadable = (
        "this Ultimate did not answer the UCI palette read (firmware before "
        "3.15a, a C64 Ultimate on 1.1.0, or the Command Interface turned off)"
    )

    def __init__(self, api: Ultimate64API) -> None:
        self._api = api

    def read_machine(self) -> np.ndarray | None:
        from .hw_provision import read_active_palette

        for _ in range(_PUSH_ATTEMPTS):
            machine = read_active_palette(self._api)
            if machine is not None:
                return np.asarray(machine, dtype=np.uint8)
        return None

    def watch_resets(self, callback: Callable[[], None]) -> None:
        self._api.add_reset_listener(callback)

    def unwatch_resets(self, callback: Callable[[], None]) -> None:
        self._api.remove_reset_listener(callback)

    def push(self, table_bgr: np.ndarray) -> bool:
        rgb = _to_rgb(table_bgr)
        return any(uci.set_palette_rgb(self._api, rgb) for _ in range(_PUSH_ATTEMPTS))

    def load(self, table_bgr: np.ndarray, stop: Callable[[], bool]) -> bool:
        return uci.load_palette(self._api, _to_rgb(table_bgr), stop=stop)

    def go(self) -> None:
        uci.go(self._api)

    def ack(self) -> None:
        uci.ack(self._api)

    def abort(self) -> None:
        uci.abort(self._api)


class KawariPaletteWriter:
    """A VIC-II Kawari with a configurable RGB palette, through its overlay
    registers. Every push is read back, because the TeensyROM+ link absorbs a
    failed write rather than raising.

    A shot's table is staged in video memory by `load` and copied into the
    palette by `go` in the raster window where the double buffer flips, so
    the colors change with the picture.

    Its palette survives a C64 reset, so there are no resets to watch, and it
    survives a run that dies too. Each push therefore records what the
    machine may show and the palette the run found
    (`kawari_palette_record_path`), and the next run's
    `put_back_a_dead_runs_kawari_palette` acts on it."""

    device = "the Kawari"
    channel_bits = 6
    load_time_s = 0.05
    ack_delay_s = 0.0
    unreadable = "the Kawari did not take back the palette a run that ended early found"

    def __init__(self, chip: kawari.Kawari, record: Path, *, reads_track_the_beam: bool) -> None:
        self.follows_shots = (
            reads_track_the_beam and chip.info.version >= kawari.OVERLAY_COPY_FIRMWARE
        )
        self._chip = chip
        self._record = record
        self._machine = _kawari_bgr(chip)
        # What the Kawari may be showing: one table, or more while a copy's
        # outcome is unknown.
        self._may_show = [self._machine.copy()]
        self._staged: np.ndarray | None = None

    def read_machine(self) -> np.ndarray | None:
        # A record that still matches means the put-back at detection failed:
        # what the Kawari shows is a dead run's, and recording it as the
        # machine's would overwrite the only copy of the real one.
        if _left_by_a_run(self._record, self._machine) is not None:
            return None
        return self._machine.copy()

    def watch_resets(self, callback: Callable[[], None]) -> None:
        pass

    def unwatch_resets(self, callback: Callable[[], None]) -> None:
        pass

    def push(self, table_bgr: np.ndarray) -> bool:
        table = np.asarray(table_bgr, dtype=np.uint8).reshape(16, 3)
        if not _write_kawari(self._chip, table):
            log.debug("hardware_palette: the Kawari's palette did not read back as written")
            return False
        self._may_show = [table]
        self._note(table)
        return True

    def load(self, table_bgr: np.ndarray, stop: Callable[[], bool]) -> bool:
        table = np.asarray(table_bgr, dtype=np.uint8).reshape(16, 3)
        self._note(*self._may_show, table)
        self._staged = table
        return self._chip.stage_palette(_kawari_channels(table), stop)

    def go(self) -> None:
        self._chip.fire_palette()

    def ack(self) -> None:
        staged, self._staged = self._staged, None
        if staged is None:
            raise RuntimeError("nothing was loaded to acknowledge")
        if not self._chip.check_palette(_kawari_channels(staged)):
            raise RuntimeError("the Kawari's palette did not read back as the shot's table")
        self._may_show = [staged]
        self._note(staged)

    def abort(self) -> None:
        # An abort after `go` leaves the staged table possibly showing.
        staged, self._staged = self._staged, None
        if staged is not None:
            self._may_show.append(staged)
        self._chip.drop_palette()

    def _note(self, *shows: np.ndarray) -> None:
        from c64cast.control.transport import atomic_write_text

        if all(np.array_equal(table, self._machine) for table in shows):
            _forget(self._record)
            return
        record = {
            "machine": self._machine.tolist(),
            "shows": [table.tolist() for table in shows],
        }
        try:
            self._record.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._record, json.dumps(record))
        except OSError as e:
            log.warning(
                "hardware_palette: could not record the Kawari's palette in %s (%s); "
                "a run that ends early will leave its colors on the machine",
                self._record,
                e,
            )


def put_back_a_dead_runs_kawari_palette(chip: kawari.Kawari, record: Path) -> np.ndarray | None:
    """The palette (BGR) a run that ended early found on the Kawari, once the
    Kawari shows it again; None when no run left its colors there, or when the
    Kawari would not take it back, in which case the record stays."""
    left = _left_by_a_run(record, _kawari_bgr(chip))
    if left is None:
        return None
    log.warning(
        "hardware_palette: the Kawari still shows the colors a run that "
        "ended early pushed; putting back the palette that run found"
    )
    if not _write_kawari(chip, left):
        log.warning("hardware_palette: the Kawari did not take that palette back")
        return None
    _forget(record)
    return left


def _kawari_bgr(chip: kawari.Kawari) -> np.ndarray:
    rgb = np.asarray(chip.info.palette_rgb, dtype=np.uint8).reshape(16, 3)
    return rgb[:, ::-1].copy()


def _kawari_channels(table_bgr: np.ndarray) -> list[int]:
    return [kawari.to_6bit(int(v)) for row in table_bgr for v in row[::-1]]


def _write_kawari(chip: kawari.Kawari, table_bgr: np.ndarray) -> bool:
    channels = _kawari_channels(table_bgr)
    return chip.write_palette(channels) == tuple(channels)


def _left_by_a_run(record: Path, found: np.ndarray) -> np.ndarray | None:
    try:
        saved = json.loads(record.read_text())
        shows = np.asarray(saved["shows"], dtype=np.uint8).reshape(-1, 16, 3)
        machine = np.asarray(saved["machine"], dtype=np.uint8).reshape(16, 3)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.debug("hardware_palette: unreadable %s ignored", record, exc_info=True)
        return None
    if not any(np.array_equal(table, found) for table in shows):
        # The machine was power-cycled, or its palette changed, since.
        _forget(record)
        return None
    return machine


def _forget(record: Path) -> None:
    try:
        record.unlink(missing_ok=True)
    except OSError as e:
        log.debug("hardware_palette: could not remove %s (%s)", record, e)


class HardwarePalette:
    """Which 16 colors the machine shows, for one run.

    `machine` is the palette the machine shows at the run's start, read back
    by the writer; it is what every restore pushes. On an Ultimate that is
    after the startup reset, because the firmware's own RESET_PALETTE restores
    its built-in table rather than a loaded .vpl. The
    quantizer's palette moves with every push (`set_host_palette`), so the
    render pipeline always aims at what the machine emits.

    Two tables carry the state. `_shown` is the table a scene asked for, or
    None for the machine's own; it is what the quantizer aims at. `_on_machine`
    is the table the machine is showing, or None when that is unknown because
    a push failed partway and the machine's answer was lost. They differ only
    between a scene's `release` and the next scene's setup, where nothing
    renders: deferring the push there is what lets a scene set up again, or
    followed by one that shows the same colors, cost no push at all.

    Every method is safe to call from the playlist thread while a reset
    listener runs on another, and none of them raises.
    """

    def __init__(self, api: C64Backend, writer: PaletteWriter, machine_bgr: np.ndarray) -> None:
        from c64cast.video.palette import C64_PALETTE_BGR, active_host_palette_name

        self._api = api
        self._writer = writer
        self._machine = np.asarray(machine_bgr, dtype=np.uint8).reshape(16, 3).copy()
        self._base_table = C64_PALETTE_BGR.copy()
        self._base_name = active_host_palette_name()
        self._shown: np.ndarray | None = None
        self._on_machine: np.ndarray | None = self._machine.copy()
        self._enabled = True
        self._lock = threading.Lock()

    @property
    def machine_palette(self) -> np.ndarray:
        """The machine's own 16 colors (BGR uint8), read after the startup reset."""
        return self._machine.copy()

    @property
    def channel_bits(self) -> int:
        return self._writer.channel_bits

    @property
    def load_time_s(self) -> float:
        return self._writer.load_time_s

    @property
    def ack_delay_s(self) -> float:
        return self._writer.ack_delay_s

    @property
    def follows_shots(self) -> bool:
        return self._writer.follows_shots

    def show(self, table_bgr: np.ndarray, scene: str) -> bool:
        """Push `table_bgr` and point the quantizer at it. False when the
        machine did not take it, in which case the run's base palette stays in
        effect and pushing is off for the rest of the run."""
        table = np.asarray(table_bgr, dtype=np.uint8).reshape(16, 3)
        with self._lock:
            if not self._enabled:
                return False
            if not self._machine_shows(table):
                self._on_machine = None
                if not self._push(table):
                    # The machine may still show an earlier scene's table, since
                    # a scene's teardown leaves it there.
                    self._put_machine_back()
                    self._give_up(f"{scene}: the palette push failed")
                    return False
                self._on_machine = table.copy()
                log.info("%s: hardware_palette -> pushed %s", scene, _table_name(table))
            if self._shown is None or not np.array_equal(table, self._shown):
                self._shown = table.copy()
                self._set_host(table, _table_name(table))
            return True

    def release(self) -> None:
        """A scene that showed its own colors is done. The quantizer goes back
        to the machine's palette now; the machine itself is left alone until
        the next scene sets up (`settle_for`), which either shows its own
        colors or puts the machine's back."""
        with self._lock:
            self._reset_host()

    def show_machine(self) -> None:
        """Put the machine's own palette back, on the machine and in the
        quantizer."""
        with self._lock:
            self._reset_host()
            if self._enabled and not self._put_machine_back():
                self._give_up("putting the machine's own palette back failed")

    def after_reset(self) -> None:
        """A C64 reset re-applies the configured palette, so re-push the
        scene's palette when one is showing. Registered with the writer's
        `watch_resets`."""
        with self._lock:
            self._on_machine = self._machine.copy()
            if self._shown is None or not self._enabled:
                return
            self._on_machine = None
            if self._push(self._shown):
                self._on_machine = self._shown.copy()
            else:
                self._give_up("the re-push after a C64 reset failed")

    def restore(self) -> None:
        """Teardown: put the machine's own palette back and detach from the
        API. Tried even after pushing was given up on, since a push that
        failed may still have landed."""
        self._writer.unwatch_resets(self.after_reset)
        if self._api.hardware_palette is self:
            self._api.hardware_palette = None
        with self._lock:
            self._enabled = False
            self._reset_host()
            if self._machine_shows(self._machine):
                return
            device = self._writer.device
            if self._put_machine_back():
                log.info("hardware_palette: restored %s's own palette", device)
            else:
                log.warning(
                    "hardware_palette: could not put %s's own palette back; "
                    "its next reset or power-cycle will",
                    device,
                )

    # The split push `ShotPusher` drives at a video's shot cuts.

    def load(self, table_bgr: np.ndarray, stop: Callable[[], bool]) -> bool:
        """Load `table_bgr` for a later `go`. False when pushing is off, when
        `stop` cut the load short, or when the link failed under it."""
        with self._lock:
            if not self._enabled:
                return False
            return self._blind("loading a shot's palette", self._writer.load, table_bgr, stop)

    def adopt(self, table_bgr: np.ndarray) -> bool:
        """Point the quantizer at the loaded table, for the frame that is
        about to render; `go` then makes the machine show it. False when
        pushing is off."""
        table = np.asarray(table_bgr, dtype=np.uint8).reshape(16, 3)
        with self._lock:
            if not self._enabled:
                return False
            self._on_machine = table.copy()
            if self._shown is None or not np.array_equal(table, self._shown):
                self._shown = table.copy()
                self._set_host(table, _table_name(table))
            return True

    def go(self) -> None:
        with self._lock:
            self._blind("pushing a shot's palette", self._writer.go)

    def ack(self) -> None:
        with self._lock:
            self._blind("acknowledging a shot's palette", self._writer.ack)

    def abort(self) -> None:
        with self._lock:
            self._blind("dropping a shot's palette", self._writer.abort)

    def _blind(self, what: str, op: Callable[..., object], *args: object) -> bool:
        try:
            done = op(*args)
        except Exception:
            log.debug("hardware_palette: %s failed", what, exc_info=True)
            self._on_machine = None
            self._give_up(f"{what} failed")
            return False
        return done is not False

    def _machine_shows(self, table: np.ndarray) -> bool:
        return self._on_machine is not None and np.array_equal(table, self._on_machine)

    def _put_machine_back(self) -> bool:
        """Push the machine's own palette unless it is known to be showing.
        False when the push failed, leaving what the machine shows unknown."""
        if self._machine_shows(self._machine):
            return True
        self._on_machine = None
        if not self._push(self._machine):
            return False
        self._on_machine = self._machine.copy()
        return True

    def _reset_host(self) -> None:
        if self._shown is not None:
            self._shown = None
            self._set_host(self._base_table, self._base_name)

    def _push(self, table_bgr: np.ndarray) -> bool:
        return self._writer.push(table_bgr)

    def _give_up(self, what: str) -> None:
        self._enabled = False
        self._reset_host()
        log.warning(
            "hardware_palette: %s; showing %s's own palette for the rest of the run",
            what,
            self._writer.device,
        )

    @staticmethod
    def _set_host(table_bgr: np.ndarray, name: str) -> None:
        from c64cast.video.palette import set_host_palette

        set_host_palette(table_bgr, name=name)


class ShotPusher:
    """Has the machine change colors on the first frame of each `PaletteShot`.

    A worker thread fits each stretch the tracker hands over (`ready`, demux
    thread), then loads the tables one at a time, in order, ahead of their
    first frames. The render thread calls `before_render` with the stretch
    whose first frame is about to render, and `after_render` once that frame
    is written: a loaded table is adopted by the quantizer before the render
    and pushed after it, so both change on the same frame. A table not yet
    loaded when its frame comes is pushed on the first frame after it is. The
    worker acknowledges a push the writer's `ack_delay_s` after it went, and
    only then loads the next table, since the Ultimate's interface holds one
    command."""

    def __init__(self, control: HardwarePalette) -> None:
        self._control = control
        self._cond = threading.Condition()
        self._to_fit: deque[PaletteShot] = deque()
        self._to_load: deque[PaletteShot] = deque()
        self._loaded: PaletteShot | None = None
        self._due: PaletteShot | None = None
        self._committing: PaletteShot | None = None
        self._went: float | None = None
        # Stretches numbered below this were queued before a seek.
        self._floor_seq = -1
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="palette-shots", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and drop whatever is loaded or awaiting its ack, so
        the interface is free for the scene's own pushes."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join()
        with self._cond:
            outstanding = self._loaded is not None or self._went is not None
            self._loaded = self._went = None
        if outstanding:
            self._control.abort()

    def ready(self, shot: PaletteShot) -> None:
        with self._cond:
            if shot.starts_over:
                self._floor_seq = shot.seq
                self._to_fit.clear()
                self._to_load.clear()
            self._to_fit.append(shot)
            self._cond.notify_all()

    def wait_loaded(self, timeout_s: float) -> bool:
        """Block until a table is loaded and waiting for its frame, so the
        first frame of a video can show its own colors."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while self._loaded is None and not self._stopping:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return self._loaded is not None

    def before_render(self, shot: PaletteShot | None) -> bool:
        """`shot` is the stretch whose first frame is about to render, if any.
        True when this render carries a push: the caller then owes
        `after_render`, whether or not the render succeeds."""
        with self._cond:
            if shot is not None:
                self._due = shot
                self._cond.notify_all()
            loaded = self._loaded
            if loaded is None or loaded is not self._due:
                return False
            fit = loaded.fit
            self._loaded = self._due = None
            self._committing = loaded
        if fit is None or not self._control.adopt(fit[0]):
            with self._cond:
                self._committing = None
            return False
        return True

    def after_render(self) -> None:
        with self._cond:
            if self._committing is None:
                return
        self._control.go()
        with self._cond:
            self._committing = None
            self._went = time.monotonic()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                action = None
                while not self._stopping and (action := self._next_action()) is None:
                    self._cond.wait(self._wait_s())
                if action is None:
                    return
            action()

    def _wait_s(self) -> float | None:
        if self._went is None:
            return None
        return max(0.0, self._went + self._control.ack_delay_s - time.monotonic())

    def _superseded(self, shot: PaletteShot) -> bool:
        due = self._due
        return shot.seq < self._floor_seq or (due is not None and due.seq > shot.seq)

    def _next_action(self) -> Callable[[], None] | None:
        if self._to_fit:
            return self._fit_next
        if self._went is not None:
            return self._ack if self._wait_s() == 0.0 else None
        if self._loaded is not None:
            return self._drop_loaded if self._superseded(self._loaded) else None
        while self._to_load and self._superseded(self._to_load[0]):
            self._to_load.popleft()
        if self._to_load and self._committing is None:
            return self._load_next
        return None

    def _fit_next(self) -> None:
        from c64cast.video.palette import derive_hardware_palette, hardware_palette_error

        with self._cond:
            shot = self._to_fit.popleft()
        table = derive_hardware_palette(
            shot.frames, self._control.machine_palette, channel_bits=self._control.channel_bits
        )
        if table is None:
            return
        baseline = float(np.mean([hardware_palette_error(f, table) for f in shot.frames]))
        shot.fitted(table, baseline)
        shot.frames = []
        with self._cond:
            self._to_load.append(shot)

    def _ack(self) -> None:
        self._control.ack()
        with self._cond:
            self._went = None

    def _drop_loaded(self) -> None:
        self._control.abort()
        with self._cond:
            self._loaded = None

    def _load_next(self) -> None:
        with self._cond:
            shot = self._to_load.popleft()
        fit = shot.fit
        assert fit is not None

        def give_up() -> bool:
            with self._cond:
                return self._stopping or self._superseded(shot)

        if self._control.load(fit[0], give_up):
            with self._cond:
                self._loaded = shot
                self._cond.notify_all()
        else:
            self._control.abort()


def settle_for(api: object, scene: object) -> None:
    """Before `scene` sets up: put the machine's own palette back, unless the
    scene shows colors of its own (`pushes_hardware_palette`), in which case
    its setup pushes them, or pushes nothing when they are already showing."""
    control = getattr(api, "hardware_palette", None)
    if not isinstance(control, HardwarePalette):
        return
    if getattr(scene, "pushes_hardware_palette", False) is True:
        return
    control.show_machine()


def _scene_colors(cfg: Config) -> list[tuple[str, ColorCfg]]:
    out = []
    for _, s in scene_and_clip_cfgs(cfg):
        try:
            out.append((s.type, scene_color(cfg, s)))
        except ValueError:
            continue
    return out


def asks_for_source(cfg: Config) -> bool:
    """Whether some scene names hardware_palette = "source" itself, rather
    than "auto", so a palette it cannot get is worth a warning."""
    return any(c.hardware_palette == "source" for _, c in _scene_colors(cfg))


def wanting_scene_types(cfg: Config) -> list[str]:
    """The type of every scene and clip that can push a palette and whose
    effective `[color]` asks for one. Scenes that name "source" but cannot
    push are named in an info line."""
    from c64cast.video.palette import HARDWARE_PALETTE_SCENE_TYPES, wants_hardware_palette

    colors = _scene_colors(cfg)
    out = [t for t, c in colors if wants_hardware_palette(c)]
    unsupported = sorted(
        {t for t, c in colors if c.hardware_palette == "source"} - set(HARDWARE_PALETTE_SCENE_TYPES)
    )
    if unsupported:
        log.info(
            "hardware_palette = source applies to video and slideshow scenes; "
            "%s scenes show the machine's own palette",
            ", ".join(unsupported),
        )
    return [t for t in out if t in HARDWARE_PALETTE_SCENE_TYPES]


def provision_hardware_palette(
    api: object, cfg: Config, *, is_ensemble: bool
) -> HardwarePalette | None:
    """Set the run up to push palettes, when any scene asks for it.

    The machine's `PaletteWriter` comes from `_writer_for`. Reading the
    machine's palette is both the capability probe and the restore snapshot:
    firmware without the UCI palette commands (before 3.15a, and the C64
    Ultimate as of 1.1.0) answers "21,UNKNOWN COMMAND", and the run then
    renders exactly as it would without the setting, after one warning.
    Returns the controller, also installed as `api.hardware_palette` for the
    scenes, or None.
    """
    if not wanting_scene_types(cfg):
        return None
    skip = None
    writer = _writer_for(api)
    if writer is None:
        skip = (
            "only an Ultimate 64, or a VIC-II Kawari with a configurable "
            "palette, can change the colors it emits"
        )
    elif cfg.debug.skip_probe:
        skip = "--skip-probe forbids reading the machine's palette first"
    elif is_ensemble:
        skip = "an ensemble shares one color pipeline across machines"
    level = logging.WARNING if asks_for_source(cfg) else logging.INFO
    if skip is not None:
        log.log(level, "hardware_palette: %s — skipped.", skip)
        return None
    assert writer is not None
    backend = cast("C64Backend", api)

    machine = writer.read_machine()
    if machine is None:
        log.log(
            level,
            "hardware_palette: %s — not pushing palettes; colors are matched "
            "against [hardware].host_palette as before.",
            writer.unreadable,
        )
        return None

    control = HardwarePalette(backend, writer, machine)
    writer.watch_resets(control.after_reset)
    backend.hardware_palette = control
    log.info(
        "hardware_palette = source: video/slideshow scenes push their own 16 "
        "colors; the machine's palette is restored for every other scene and at exit"
    )
    return control


def _writer_for(api: object) -> PaletteWriter | None:
    from c64cast.app.paths import kawari_palette_record_path

    from .api import Ultimate64API

    if isinstance(api, Ultimate64API) and api.profile.supports_system_mode:
        return UltimatePaletteWriter(api)
    chip = getattr(api, "kawari", None)
    if isinstance(chip, kawari.Kawari) and chip.info.configurable_rgb:
        # `go` times its copy off a raster read. A TeensyROM+ read lands within
        # a few lines; an Ultimate's REST read takes about two frames, so it
        # is stale by the time the copy fires, and a third of the copies tore (measured).
        tr_link = getattr(getattr(api, "profile", None), "family", None) == "tr"
        return KawariPaletteWriter(chip, kawari_palette_record_path(), reads_track_the_beam=tr_link)
    return None


def restore_hardware_palette(control: HardwarePalette | None) -> None:
    """Teardown half of `provision_hardware_palette`. No-op without one."""
    if control is not None:
        control.restore()
