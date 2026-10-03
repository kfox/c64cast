"""`[color].hardware_palette`: pushing a scene's own 16 colors to an Ultimate 64.

See docs/architecture/video-color.md#hardware_palettepy--pushing-a-scenes-own-16-colors-colorhardware_palette.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from c64cast.app.config import Config, clip_scene_cfg, scene_color
from c64cast.hw import uci

if TYPE_CHECKING:
    from .api import Ultimate64API

log = logging.getLogger(__name__)

# A transaction that starts on a DMA socket the firmware has just closed loses
# its first byte, and the command fails once and then works.
_PUSH_ATTEMPTS = 2


def _to_rgb(table_bgr: np.ndarray) -> list[tuple[int, int, int]]:
    return [(int(r), int(g), int(b)) for b, g, r in table_bgr]


def _table_name(table_bgr: np.ndarray) -> str:
    digest = hashlib.sha256(np.asarray(table_bgr, dtype=np.uint8).tobytes()).hexdigest()
    return f"hw-source:{digest[:6]}"


class HardwarePalette:
    """Which 16 colors the machine shows, for one run.

    `machine` is the palette the Ultimate was showing before the run, read back
    over UCI; it is what every restore pushes, because the firmware's own
    RESET_PALETTE restores its built-in table rather than a loaded .vpl. The
    quantizer's palette moves with every push (`set_host_palette`), so the
    render pipeline always aims at what the machine emits.

    Two flags carry the state. `_shown` is the table a scene asked for, or None
    for the machine's own. `_dirty` is whether the machine may be showing
    anything other than its own palette, which is what teardown checks: a push
    that failed partway counts, since the machine's answer was lost.

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
        self._dirty = False
        self._enabled = True
        self._lock = threading.Lock()

    @property
    def machine_palette(self) -> np.ndarray:
        """The machine's own 16 colors (BGR uint8), read before the run."""
        return self._machine.copy()

    def show(self, table_bgr: np.ndarray, scene: str) -> bool:
        """Push `table_bgr` and point the quantizer at it. False when the
        machine did not take it, in which case the run's base palette stays in
        effect and pushing is off for the rest of the run."""
        table = np.asarray(table_bgr, dtype=np.uint8).reshape(16, 3)
        with self._lock:
            if not self._enabled:
                return False
            if self._shown is not None and np.array_equal(table, self._shown):
                return True
            self._dirty = True
            if not self._push(table):
                self._give_up(f"{scene}: the palette push failed")
                return False
            self._shown = table.copy()
            self._set_host(table, _table_name(table))
            log.info("%s: hardware_palette -> pushed %s", scene, _table_name(table))
            return True

    def show_machine(self) -> None:
        """Put the machine's own palette back, on the machine and in the
        quantizer."""
        with self._lock:
            self._show_machine_locked()

    def _show_machine_locked(self) -> None:
        if self._shown is not None:
            self._shown = None
            self._set_host(self._base_table, self._base_name)
        if not self._dirty or not self._enabled:
            return
        if self._push(self._machine):
            self._dirty = False
        else:
            self._give_up("putting the machine's own palette back failed")

    def after_reset(self) -> None:
        """A C64 reset re-applies the configured palette, so re-push the
        scene's palette when one is showing. Registered on the API with
        `add_reset_listener`."""
        with self._lock:
            self._dirty = False
            if self._shown is None or not self._enabled:
                return
            self._dirty = True
            if not self._push(self._shown):
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
            if self._shown is not None:
                self._shown = None
                self._set_host(self._base_table, self._base_name)
            if not self._dirty:
                return
            if self._push(self._machine):
                self._dirty = False
                log.info("hardware_palette: restored the Ultimate's own palette")
            else:
                log.warning(
                    "hardware_palette: could not put the Ultimate's own palette "
                    "back; its next reset or power-cycle will"
                )

    def _push(self, table_bgr: np.ndarray) -> bool:
        rgb = _to_rgb(table_bgr)
        return any(uci.set_palette_rgb(self._api, rgb) for _ in range(_PUSH_ATTEMPTS))

    def _give_up(self, what: str) -> None:
        self._enabled = False
        if self._shown is not None:
            self._shown = None
            self._set_host(self._base_table, self._base_name)
        log.warning(
            "hardware_palette: %s; showing the Ultimate's own palette for the rest of the run",
            what,
        )

    @staticmethod
    def _set_host(table_bgr: np.ndarray, name: str) -> None:
        from c64cast.video.palette import set_host_palette

        set_host_palette(table_bgr, name=name)


def wanting_scene_types(cfg: Config) -> list[str]:
    """The type of every scene and clip that can push a palette and whose
    effective `[color]` asks for one. Scenes that ask but cannot are named in
    an info line."""
    from c64cast.video.palette import HARDWARE_PALETTE_SCENE_TYPES

    candidates = list(cfg.scenes)
    for clip in cfg.performance.clips:
        try:
            candidates.append(clip_scene_cfg(clip))
        except ValueError:
            continue
    out = []
    for s in candidates:
        try:
            wants = scene_color(cfg, s).hardware_palette == "source"
        except ValueError:
            continue
        if wants:
            out.append(s.type)
    unsupported = sorted({t for t in out if t not in HARDWARE_PALETTE_SCENE_TYPES})
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
    restore snapshot: firmware without the UCI palette commands (before 3.15,
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
    if skip is not None:
        log.warning("hardware_palette = source: %s — skipped.", skip)
        return None
    assert isinstance(api, Ultimate64API)

    machine = None
    for _ in range(_PUSH_ATTEMPTS):
        machine = read_active_palette(api)
        if machine is not None:
            break
    if machine is None:
        log.warning(
            "hardware_palette = source: this Ultimate did not answer the UCI "
            "palette read (firmware before 3.15, a C64 Ultimate on 1.1.0, or "
            "the Command Interface turned off) — not pushing palettes; colors "
            "are matched against [hardware].host_palette as before."
        )
        return None

    control = HardwarePalette(api, np.asarray(machine, dtype=np.uint8))
    api.add_reset_listener(control.after_reset)
    api.hardware_palette = control
    log.info(
        "hardware_palette = source: video/slideshow scenes push their own 16 "
        "colors; the machine's palette is restored after each scene and at exit"
    )
    return control


def restore_hardware_palette(control: HardwarePalette | None) -> None:
    """Teardown half of `provision_hardware_palette`. No-op without one."""
    if control is not None:
        control.restore()
