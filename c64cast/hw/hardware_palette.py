"""`[color].hardware_palette`: pushing a scene's own 16 colors to an Ultimate 64.

See docs/architecture/video-color.md#hardware_palettepy--pushing-a-scenes-own-16-colors-colorhardware_palette.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from c64cast.app.config import ColorCfg, Config, scene_and_clip_cfgs, scene_color
from c64cast.hw import uci

if TYPE_CHECKING:
    from c64cast.video.palette_shots import PaletteShot

    from .api import Ultimate64API

log = logging.getLogger(__name__)

# The DMA client redials a connection the firmware dropped, but cannot resend
# the commands that connection may have discarded, so a push that went out
# on it fails once.
_PUSH_ATTEMPTS = 2


def _to_rgb(table_bgr: np.ndarray) -> list[tuple[int, int, int]]:
    return [(int(r), int(g), int(b)) for b, g, r in table_bgr]


def _table_name(table_bgr: np.ndarray) -> str:
    digest = hashlib.sha256(np.asarray(table_bgr, dtype=np.uint8).tobytes()).hexdigest()
    return f"hw-source:{digest[:6]}"


class HardwarePalette:
    """Which 16 colors the machine shows, for one run.

    `machine` is the palette the Ultimate shows after the run's startup reset, read back
    over UCI; it is what every restore pushes, because the firmware's own
    RESET_PALETTE restores its built-in table rather than a loaded .vpl. The
    quantizer's palette moves with every push (`set_host_palette`), so the
    render pipeline always aims at what the machine emits.

    Two tables carry the state. `_shown` is the table a scene asked for, or
    None for the machine's own; it is what the quantizer aims at. `_on_machine`
    is the table the Ultimate is showing, or None when that is unknown because
    a push failed partway and the machine's answer was lost. They differ only
    between a scene's `release` and the next scene's setup, where nothing
    renders: deferring the push there is what lets a scene set up again, or
    followed by one that shows the same colors, cost no push at all.

    Every method is safe to call from the playlist thread while a reset
    listener runs on another, and none of them raises.
    """

    def __init__(self, api: Ultimate64API, machine_bgr: np.ndarray) -> None:
        from c64cast.video.palette import C64_PALETTE_BGR, active_host_palette_name

        self._api = api
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
        scene's palette when one is showing. Registered on the API with
        `add_reset_listener`."""
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
        self._api.remove_reset_listener(self.after_reset)
        if self._api.hardware_palette is self:
            self._api.hardware_palette = None
        with self._lock:
            self._enabled = False
            self._reset_host()
            if self._machine_shows(self._machine):
                return
            if self._put_machine_back():
                log.info("hardware_palette: restored the Ultimate's own palette")
            else:
                log.warning(
                    "hardware_palette: could not put the Ultimate's own palette "
                    "back; its next reset or power-cycle will"
                )

    # The blind, split push `ShotPusher` drives at a video's shot cuts. None
    # of these reads anything back: see `uci.load_palette`.

    def load(self, table_bgr: np.ndarray, stop: Callable[[], bool]) -> bool:
        """Load `table_bgr` for a later `go`. False when pushing is off, when
        `stop` cut the load short, or when the link failed under it."""
        with self._lock:
            if not self._enabled:
                return False
            return self._blind(
                "loading a shot's palette", uci.load_palette, _to_rgb(table_bgr), stop=stop
            )

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
            self._blind("pushing a shot's palette", uci.go)

    def ack(self) -> None:
        with self._lock:
            self._blind("acknowledging a shot's palette", uci.ack)

    def abort(self) -> None:
        with self._lock:
            self._blind("dropping a shot's palette", uci.abort)

    def _blind(self, what: str, op: Callable[..., object], *args: object, **kwargs: object) -> bool:
        try:
            done = op(self._api, *args, **kwargs)
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
        rgb = _to_rgb(table_bgr)
        return any(uci.set_palette_rgb(self._api, rgb) for _ in range(_PUSH_ATTEMPTS))

    def _give_up(self, what: str) -> None:
        self._enabled = False
        self._reset_host()
        log.warning(
            "hardware_palette: %s; showing the Ultimate's own palette for the rest of the run",
            what,
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
    worker acknowledges a push ACK_DELAY_S after it went, and only then loads
    the next table, since the interface holds one command."""

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
        return max(0.0, self._went + uci.ACK_DELAY_S - time.monotonic())

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
        table = derive_hardware_palette(shot.frames, self._control.machine_palette)
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

    Reading the machine's palette is both the capability probe and the
    restore snapshot: firmware without the UCI palette commands (before 3.15a,
    and the C64 Ultimate as of 1.1.0) answers "21,UNKNOWN COMMAND", and the run
    then renders exactly as it would without the setting, after one warning.
    Returns the controller, also installed as `api.hardware_palette` for the
    scenes, or None.
    """
    from .api import Ultimate64API
    from .hw_provision import read_active_palette

    if not wanting_scene_types(cfg):
        return None
    skip = None
    if not isinstance(api, Ultimate64API) or not api.profile.supports_system_mode:
        skip = "only an Ultimate 64 can change the colors it emits"
    elif cfg.debug.skip_probe:
        skip = "--skip-probe forbids reading the machine's palette first"
    elif is_ensemble:
        skip = "an ensemble shares one color pipeline across machines"
    level = logging.WARNING if asks_for_source(cfg) else logging.INFO
    if skip is not None:
        log.log(level, "hardware_palette: %s — skipped.", skip)
        return None
    assert isinstance(api, Ultimate64API)

    machine = None
    for _ in range(_PUSH_ATTEMPTS):
        machine = read_active_palette(api)
        if machine is not None:
            break
    if machine is None:
        log.log(
            level,
            "hardware_palette: this Ultimate did not answer the UCI "
            "palette read (firmware before 3.15a, a C64 Ultimate on 1.1.0, or "
            "the Command Interface turned off) — not pushing palettes; colors "
            "are matched against [hardware].host_palette as before.",
        )
        return None

    control = HardwarePalette(api, np.asarray(machine, dtype=np.uint8))
    api.add_reset_listener(control.after_reset)
    api.hardware_palette = control
    log.info(
        "hardware_palette = source: video/slideshow scenes push their own 16 "
        "colors; the machine's palette is restored for every other scene and at exit"
    )
    return control


def restore_hardware_palette(control: HardwarePalette | None) -> None:
    """Teardown half of `provision_hardware_palette`. No-op without one."""
    if control is not None:
        control.restore()
