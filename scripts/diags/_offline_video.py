"""Decode a video window the way playback does and run it through the
production mhires path into a software VIC, with no hardware.

`iter_frames` decodes like `AVFileSource`: the same decode-size plan and the
same swscale downscale, with an optional `interpolation` override so a filter
can be compared. It can also hand back a reference for each frame, decoded at
full resolution, cropped the way the scene crops, and area-resized to 320x200.

`OfflineMhires` builds the display mode through the same resolver a config's
video scene goes through, then repeats what `VideoScene.process_frame` does
from the crop on: the online auto-fit warm-up, then `_render_with_overlays`.
Its backend has no REU and double buffering is off, so frames are pushed in
place over host DMA, the path whose writes the framebuffer sees and whose
per-frame delta is exactly what changed on screen. Flicker blending brings
its own bank swap regardless.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import av
import cv2
import numpy as np

from c64cast.app.config import ColorCfg, Config, SceneCfg
from c64cast.app.scene_factory import _display_mode_for_scene
from c64cast.hw.backend import ULTIMATE_PROFILE
from c64cast.hw.c64 import VIC
from c64cast.scenes.scenes import ONLINE_FIT_WARMUP_FRAMES, _crop_to_aspect, _render_with_overlays
from c64cast.video.framebuffer import Framebuffer
from c64cast.video.modes.mhires import MultiHiresDisplayMode
from c64cast.video.modes_irq import FRAME_TRACKER_ADDR, HOSTDMA_TRACKER_OFF_BG0
from c64cast.video.palette import ColorFit, ColorFitAccumulator
from c64cast.video.video import _DecodePlan, _frame_to_scan_bgr, _trim_to_crop
from scripts.diags.render_offline import RenderBackend

MHIRES_TARGET = (160, 200)
REFERENCE_SIZE = (320, 200)


@dataclass
class Tick:
    index: int
    t: float
    frame: np.ndarray
    reference: np.ndarray | None = None


def _production_bgr(frame: Any, plan: _DecodePlan, interpolation: str | None) -> np.ndarray:
    if interpolation is None:
        img = _frame_to_scan_bgr(frame, plan)
    else:
        plan.update(frame)
        if plan.size is None:
            img = frame.to_ndarray(format="bgr24")
        else:
            img = frame.reformat(
                width=plan.size[0], height=plan.size[1], format="bgr24", interpolation=interpolation
            ).to_ndarray()
    return _trim_to_crop(img)


def reference_frame(full_bgr: np.ndarray) -> np.ndarray:
    return cv2.resize(_crop_to_aspect(full_bgr), REFERENCE_SIZE, interpolation=cv2.INTER_AREA)


def iter_frames(
    path: str,
    t0: float = 0.0,
    t1: float | None = None,
    *,
    fps: float | None = None,
    interpolation: str | None = None,
    reference: bool = False,
) -> Iterator[Tick]:
    """The frames playback would show from [t0, t1] seconds of `path`.

    With `fps` unset every decoded frame is shown, as on the sampler path,
    where the playlist polls faster than the source and dedups. With `fps`
    set, each tick shows the newest frame due by then and a frame repeated
    from the last tick is skipped, as `current_frame` and the dedup do on a
    capped path; frames due between two ticks are never shown."""
    container = av.open(path)
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        time_base = float(stream.time_base) if stream.time_base else 0.0
        if t0 > 0:
            container.seek(int(t0 * av.time_base))
        plan = _DecodePlan(MHIRES_TARGET)
        index = 0
        next_tick = t0
        due: tuple[float, Any] | None = None
        shown = True
        for frame in container.decode(stream):
            pts = float(frame.pts) * time_base if frame.pts is not None else 0.0
            if pts < t0:
                continue
            if t1 is not None and pts > t1:
                break
            if fps is None:
                yield _tick(index, pts, frame, plan, interpolation, reference)
                index += 1
                continue
            while pts > next_tick:
                if due is not None and not shown:
                    yield _tick(index, *due, plan, interpolation, reference)
                    index += 1
                    shown = True
                next_tick += 1.0 / fps
            due, shown = (pts, frame), False
        if due is not None and not shown:
            yield _tick(index, *due, plan, interpolation, reference)
    finally:
        container.close()


def _tick(
    index: int,
    pts: float,
    frame: Any,
    plan: _DecodePlan,
    interpolation: str | None,
    reference: bool,
) -> Tick:
    ref = reference_frame(frame.to_ndarray(format="bgr24")) if reference else None
    return Tick(index, pts, _production_bgr(frame, plan, interpolation), ref)


@dataclass
class Write:
    address: int
    size: int


@dataclass
class OfflineMhires:
    """One video scene's mhires mode on an offline backend and framebuffer.

    `fit` installs a fixed color fit instead of the online warm-up."""

    color: ColorCfg = field(default_factory=ColorCfg)
    fit: ColorFit | None = None
    writes: list[Write] = field(default_factory=list)

    def __post_init__(self) -> None:
        cfg = Config()
        cfg.color = self.color
        cfg.video.double_buffer = False
        self.mode = _display_mode_for_scene(None, SceneCfg(type="video", file="offline"), cfg)
        self.api = RenderBackend()
        self.api.profile = ULTIMATE_PROFILE
        self.framebuffer = Framebuffer()
        self.api.add_write_listener(self.framebuffer.on_write)
        self.api.add_write_listener(self._commit_staged_bg0)
        self.api.add_write_listener(lambda a, d: self.writes.append(Write(a, len(d))))
        self.mode.setup(self.api)
        self._scene = SimpleNamespace(name="offline/mhires", effects=[], overlays=[])
        self._online: ColorFitAccumulator | None = None
        self._online_frames = 0
        if self.fit is not None:
            self.mode.set_color_fit(self.fit)
        elif self.color.auto_fit and not (
            self.color.force_palette or self.color.hardware_palette != "off"
        ):
            self.mode.set_color_fit(None)
            self._online = ColorFitAccumulator(strength=1.0)

    def _commit_staged_bg0(self, address: int, data: bytes) -> None:
        """A bank-swapping push stages bg0 in the swap tracker, and the C64's
        swap IRQ copies it into $D021; the host never writes $D021 itself, so
        the framebuffer would keep showing the bg0 from setup."""
        offset = FRAME_TRACKER_ADDR + HOSTDMA_TRACKER_OFF_BG0 - address
        if 0 <= offset < len(data):
            self.framebuffer.on_write(VIC.D021_BG0, bytes([data[offset] & 0x0F]))

    def reset_pick_state(self) -> None:
        assert isinstance(self.mode, MultiHiresDisplayMode)
        self.mode._reset_pick_state()

    def restart_online_fit(self) -> None:
        """Converge a new online fit from the next frame on, as the scene does
        over its first frames."""
        if self._online is not None:
            self._online = ColorFitAccumulator(strength=1.0)
            self._online_frames = 0

    def install_fit(self, fit: ColorFit | None) -> None:
        """Hold `fit` from now on; the online warm-up, if still running, stops."""
        self._online_frames = ONLINE_FIT_WARMUP_FRAMES
        self.mode.set_color_fit(fit)

    def show(self, frame: np.ndarray, t: float) -> list[Write]:
        """Compose and push one new source frame; returns the writes it made."""
        img = _crop_to_aspect(frame)
        if self._online is not None and self._online_frames < ONLINE_FIT_WARMUP_FRAMES:
            self._online.add(img)
            self._online_frames += 1
            self.mode.set_color_fit(self._online.result())
        self.writes.clear()
        _render_with_overlays(self.mode, self.api, img, [], t, self._scene, None)  # type: ignore[arg-type]
        self._last_cropped = img
        return list(self.writes)

    def render(self) -> np.ndarray:
        return self.framebuffer.render()

    def quantizer_target(self) -> np.ndarray:
        """The last shown frame as the quantizer was asked to reproduce it:
        resized and color-shaped (auto-fit, saturation, hue bands, channel
        boost), before dither, at 320x200 with mhires' double-wide pixels.
        The shaping departs from the source on purpose, so error against this
        is the quantizer's own error."""
        small = cv2.resize(self._last_cropped, MHIRES_TARGET, interpolation=cv2.INTER_AREA)
        shaped = np.clip(np.rint(self.mode.quantizer_input(small)), 0, 255).astype(np.uint8)
        return np.repeat(shaped, 2, axis=1)


def link_cost_s(writes: list[Write]) -> float:
    return sum(ULTIMATE_PROFILE.write_cost_s(w.size) for w in writes)
