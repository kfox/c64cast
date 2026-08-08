"""The `game_of_life` generative source: WLED "Game Of Life" port: Conway's Game of Life on a coarse grid (chunky upscaled cells — reads great after C64 quantization, especially on PETSCII), with WLED's signature parent-color inheritance (a newly-born cell's hue is the mean of its live parents' hues)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


def _life_step(grid: np.ndarray, hue: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One Conway generation (standard B3/S23, torus-wrapped via `np.roll`).
    `hue` carries per-cell color; a newly-born cell's hue is the (linear, not
    circular — a documented simplification) mean of its exactly-3 live parent
    neighbors' hues, WLED's Game of Life's "parent color inheritance" touch.
    Dead cells' hue is left stale (irrelevant — never rendered)."""
    shifts = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    neighbor_count = np.zeros_like(grid, dtype=np.int8)
    hue_sum = np.zeros_like(hue)
    alive_hue = np.where(grid, hue, 0.0)
    for dy, dx in shifts:
        shifted_alive = np.roll(np.roll(grid, dy, axis=0), dx, axis=1)
        neighbor_count += shifted_alive
        hue_sum += np.roll(np.roll(alive_hue, dy, axis=0), dx, axis=1)
    born = (~grid) & (neighbor_count == 3)
    survive = grid & ((neighbor_count == 2) | (neighbor_count == 3))
    new_grid = born | survive
    # Born cells always have exactly 3 live neighbors (the B3 rule), so the
    # accumulated hue_sum / 3 is their new hue's mean; survivors keep theirs.
    new_hue = np.where(born, hue_sum / 3.0, hue)
    return new_grid, new_hue


@register("game_of_life")
class GameOfLifeSource(GenerativeSource):
    """WLED "Game Of Life" port: Conway's Game of Life on a coarse grid
    (chunky upscaled cells — reads great after C64 quantization, especially on
    PETSCII), with WLED's signature parent-color inheritance (a newly-born
    cell's hue is the mean of its live parents' hues).

    Unlike the dot/line family (Tier 2), this is a genuinely *stateful*
    simulation — generation N can't be computed without generation N-1 — so it
    can't be a closed-form function of `t` the way plasma/tunnel are. It stays
    a **pure** function of `t` anyway (unlike `SoapSource`/`FireworksSource`)
    by replaying the whole simulation from a fixed-seed initial soup
    for `floor(t / STEP_S)` generations every time it's asked for a frame —
    the same trick `mandelbrot`/`hopalong` use to stay pure despite doing real
    per-frame work. A capped `_EPOCH_GENERATIONS` bounds replay cost and
    doubles as WLED's adaptive "stagnation restart" (detecting a dead/looping
    board and reseeding) — here it's a fixed-length cycle instead of adaptive
    detection, a documented simplification in the same spirit as
    `rotozoomer`'s closed-form angle or `metaballs`' perlin substitution. An
    instance-level cache (keyed on the reachable `(epoch, generation)` pair,
    not on call order) makes sequential real playback cheap — stepping
    forward from the last-computed generation instead of replaying from
    scratch — without weakening the purity guarantee: a cache miss (a new
    epoch, or `t` landing before the cached generation) always re-derives from
    the fixed seed, so the result never depends on *how* a given `t` was
    reached, only on `t` itself."""

    LIVE_PARAMS = {"speed": (0.1, 4.0)}

    _CELL_PX = 4  # grid resolution: width/height divided by this
    _SEED = 0x60FE
    _DENSITY = 0.28  # fraction of cells alive in a fresh soup
    _STEP_S = 0.15  # seconds per generation at speed=1.0
    _EPOCH_GENERATIONS = 200  # replay cap per epoch (~hopalong's iteration budget)

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 1.0):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self._grid_w = max(4, width // self._CELL_PX)
        self._grid_h = max(4, height // self._CELL_PX)
        self._epoch_s = self._STEP_S * self._EPOCH_GENERATIONS
        self._cache_epoch: int | None = None
        self._cache_gen = -1
        self._cache_grid: np.ndarray | None = None
        self._cache_hue: np.ndarray | None = None

    def reset(self) -> None:
        self._cache_epoch = None
        self._cache_gen = -1
        self._cache_grid = None
        self._cache_hue = None

    def _seed_epoch(self, epoch: int) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(self._SEED + epoch)
        grid = rng.random((self._grid_h, self._grid_w)) < self._DENSITY
        hue = rng.random((self._grid_h, self._grid_w)).astype(np.float32)
        return grid, hue

    def _state_at(self, epoch: int, gen: int) -> tuple[np.ndarray, np.ndarray]:
        if self._cache_epoch == epoch and gen >= self._cache_gen:
            assert self._cache_grid is not None and self._cache_hue is not None
            grid, hue, cur_gen = self._cache_grid, self._cache_hue, self._cache_gen
        else:
            grid, hue = self._seed_epoch(epoch)
            cur_gen = 0
        while cur_gen < gen:
            grid, hue = _life_step(grid, hue)
            cur_gen += 1
        self._cache_epoch, self._cache_gen = epoch, gen
        self._cache_grid, self._cache_hue = grid, hue
        return grid, hue

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = max(0.0, t) * self.speed
        epoch = int(tt // self._epoch_s)
        local = tt - epoch * self._epoch_s
        gen = min(self._EPOCH_GENERATIONS, int(local // self._STEP_S))
        grid, hue = self._state_at(epoch, gen)
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        small = np.zeros((self._grid_h, self._grid_w, 3), dtype=np.uint8)
        alive_idx = np.nonzero(grid)
        if alive_idx[0].size:
            cell_hue = np.mod(hue[alive_idx] + hue_off, 1.0).astype(np.float32)
            small[alive_idx] = self._hsv_to_bgr(cell_hue[None, :], val=val)[0]
        return cv2.resize(small, (self.width, self.height), interpolation=cv2.INTER_NEAREST)
