"""80x50 multicolor character mode over an uploaded 2x2-pixel charset."""

from __future__ import annotations

import cv2
import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import RegionID
from c64cast.text_surface import CharTextSurface
from c64cast.video.dither import DITHER_METHODS, error_diffuse_cells
from c64cast.video.palette import (
    C64_PALETTE_BGR,
    COLOR_MATCH_MODES,
    PERCEPTUAL_DIST_SCALE,
    apply_color_fit,
    apply_hue_corrections,
    boost_saturation,
    build_fade_lut,
    pick_diverse_top_n,
    quantize_distances,
    quantize_distances_for,
)

from .base import (
    GRAYSCALE_MCM_BGS,
    ORDERED_DITHER_OFFSET_FNS,
    PALETTE_MODES,
    MCMComposeBuffers,
    advance_palette_cycle,
    ema_counts,
    palette_mode_settings,
    resolve_color_shaping,
    validate_palette_mode,
)
from .char import CharDisplayMode, clear_char_screen


class MCMDisplayMode(CharDisplayMode):
    """80×50 multicolor character mode using an uploaded 2×2-pixel charset.

    palette_mode (slot-allocation strategy only; color shaping is the global
    [color] stage applied to every mode):
      "cheap" — HSV saturation boost + gray-penalty bias on the per-pixel
        argmin. Fixes the typical "everything turns gray or pale cyan" failure
        mode of unbiased nearest-palette quantization without changing how the
        three global background colors are chosen.
      "vivid" — same biases, plus the 3 global backgrounds are picked by
        hue-diversity rather than raw frequency. The frame's single most
        populated palette entry always wins slot 0 (so a webcam pointed at a
        red sweater still gets red); the remaining slots prefer the most
        populated *with a hue gap* from the already-chosen chromatic picks.
      "grayscale" — fixed bg slots (dark gray / gray / light gray) in
        luminance order; FG resolves to {black, white}. Yields full 5-level
        gray coverage per screen while keeping the bg assignment stable
        across frames so the delta cache hits on every screen RAM write.
      "percell" (default) — MCM already picks the fg color per cell (1 of 8) so
        the per-cell c1/c2/c3 trick mhires uses doesn't apply here; MCM treats
        percell as "cheap". Accepted so the playlist-default palette_mode value
        works on every display mode.
    """

    name = "mcm"
    frame_target_size = (80, 50)
    # Live-tune surface (see DisplayMode.LIVE_PARAMS). MCM applies the adaptive
    # color fit, spatial dither, and nearest-palette matching, and can swap
    # palette_mode live — but has no per-cell (percell) axis.
    LIVE_PARAMS = {"dither_strength": (0.0, 2.0), "auto_fit_strength": (0.0, 1.0)}
    LIVE_CHOICES = {
        "dither_method": DITHER_METHODS,
        "color_match": COLOR_MATCH_MODES,
        "palette_mode": PALETTE_MODES,
    }

    def __init__(
        self,
        palette_mode: str = "percell",
        hue_corrections: list[dict] | None = None,
        hue_corrections_replace: bool = False,
        channel_boost: list[float] | None = None,
        force_palette: bool = False,
        dither_method: str = "none",
        dither_strength: float = 0.5,
        perceptual: bool = False,
        auto_fit_strength: float = 1.0,
    ):
        validate_palette_mode(palette_mode)
        self._auto_fit_strength = float(min(1.0, max(0.0, auto_fit_strength)))
        # The forced-palette preset pairs with percell (see cycle_style); when
        # config opts in, start in that state regardless of the configured
        # palette_mode (which still seeds the non-forced cycle stops).
        self._force_palette = bool(force_palette)
        if self._force_palette:
            palette_mode = "percell"
        self.palette_mode = palette_mode
        self._sat_factor, self._gray_penalty = palette_mode_settings(palette_mode)
        self._channel_boost, self._hue_corrections = resolve_color_shaping(
            channel_boost, hue_corrections, hue_corrections_replace
        )
        # Perceptual (CIE-Lab) nearest-palette matching ([color].color_match).
        # When on, compose() measures nearest-color in Lab (perceptually uniform)
        # instead of the brightness-weighted BGR metric. The channel_boost + gray
        # penalty shaping still applies (they keep flat desaturated regions from
        # fragmenting to gray and hold C64-friendly hues); only the distance
        # space changes. The penalty is in d² units, so it's scaled to the Lab
        # metric's smaller magnitude. See palette.quantize_distances_for.
        self._perceptual = bool(perceptual)
        self._penalty_scale = PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
        self._dither_method = dither_method
        self._dither_strength = dither_strength
        self._last_bg: np.ndarray | None = None
        # grayscale uses a fixed bg slot assignment so the per-cell screen
        # nibbles don't shuffle frame-to-frame — see GRAYSCALE_* comment up top.
        self._fixed_bg: np.ndarray | None = (
            np.array(GRAYSCALE_MCM_BGS, dtype=np.int64) if palette_mode == "grayscale" else None
        )
        # EMA-smoothed counts for cheap/vivid picks; see base.PALETTE_PICK_EMA_ALPHA.
        self._smoothed_counts: np.ndarray | None = None

    def set_palette_mode(self, api, palette_mode: str, *, force_palette: bool | None = None) -> str:
        """Apply `palette_mode` (and optionally the forced-palette flag) to the
        running instance — shared by the SHIFT cycle and the on-C64 menu. Resets
        the EMA + last-bg state and invalidates the delta cache so the next frame
        re-picks slots and fully repaints. Returns the same label the SHIFT
        cycle logs."""
        validate_palette_mode(palette_mode)
        self.palette_mode = palette_mode
        if force_palette is not None:
            self._force_palette = force_palette
        self._sat_factor, self._gray_penalty = palette_mode_settings(palette_mode)
        self._fixed_bg = (
            np.array(GRAYSCALE_MCM_BGS, dtype=np.int64) if palette_mode == "grayscale" else None
        )
        # Reset EMA + last-bg so the new mode's slot picks don't blend with
        # the previous mode's accumulated counts and so border/bg get
        # re-pushed on the next frame.
        self._smoothed_counts = None
        self._last_bg = None
        api.invalidate_cache()
        return f"palette_mode={palette_mode}" + ("+forced" if self._force_palette else "")

    def cycle_style(self, api):
        new_mode, new_force, _label = advance_palette_cycle(
            self.palette_mode, self._force_palette, self._color_map is not None
        )
        return self.set_palette_mode(api, new_mode, force_palette=new_force)

    # --- live-tune setters (see DisplayMode.LIVE_PARAMS / LIVE_CHOICES) ---
    @property
    def dither_strength(self) -> float:
        return self._dither_strength

    @dither_strength.setter
    def dither_strength(self, value: float) -> None:
        self._dither_strength = float(value)

    def set_dither_method(self, value: str) -> str:
        self._dither_method = value
        return f"dither_method={value}"

    def set_color_match(self, value: str) -> str:
        """Live-swap the nearest-palette metric; re-derive the d²-space gray-
        penalty scale to match (perceptual measures in the smaller Lab metric)."""
        self._perceptual = value == "perceptual"
        self._penalty_scale = PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
        return f"color_match={value}"

    def setup(self, api):
        super().setup(api)
        # Re-upload the charset on every setup(), not just the first. The
        # charset lives at $3000, which falls inside the $2000-$3F3F bitmap
        # area that hires/mhires scenes write to. In a looping multi-scene
        # playlist this MCMDisplayMode instance is reused across loops, so an
        # intervening bitmap scene clobbers $3000 between two appearances of
        # this scene — a one-time upload would then leave stale bitmap bytes
        # as the character set (visible as a corrupted charset). It's a single
        # 2 KB write at scene-entry time, so re-uploading is cheap.
        charset = bytearray(2048)
        for i in range(256):
            tl, tr, bl, br = (i >> 6) & 3, (i >> 4) & 3, (i >> 2) & 3, i & 3
            row_top = (tl << 6) | (tl << 4) | (tr << 2) | tr
            row_bot = (bl << 6) | (bl << 4) | (br << 2) | br
            charset[i * 8 : i * 8 + 4] = [row_top] * 4
            charset[i * 8 + 4 : i * 8 + 8] = [row_bot] * 4
        api.write_memory_file("3000", bytes(charset))
        # Clear-then-reveal (see PETSCIIDisplayMode.setup / engage_bitmap_mode):
        # screen code 0x00 selects bg slot 0 for every sub-pixel, so pinning
        # $D020-$D023 to black too guarantees that slot is actually black —
        # otherwise the reveal would show a clean-but-colored field under
        # whatever bg0-2/border the previous scene left behind. $D011 flips
        # last, once the clean black field is fully in place.
        clear_char_screen(api, screen_code=0x00)
        api.write_memory("d018", "1c")
        api.write_memory("d016", "18")
        api.write_regs("d020", 0, 0, 0, 0)
        api.write_memory("d011", "1b")
        self._last_bg = None  # force re-push of bg on first frame after setup

    def compose(self, frame) -> MCMComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)
        if self._force_palette and self._color_map is not None:
            # Forced-palette remap: emit exact C64 colors and skip the faithful
            # shaping stages + gray penalty (the remap already chose each color).
            flat = self._color_map.apply(img).reshape(-1, 3).astype(np.float32)
            all_d = quantize_distances(flat)  # (4000, 16)
        else:
            fit = self._fit_for_apply()
            if fit is not None:
                img = apply_color_fit(img, fit)
            img = boost_saturation(img, self._sat_factor)
            # Global [color] shaping: hue-band corrections then per-channel boost.
            img = apply_hue_corrections(img, self._hue_corrections)
            flat = np.clip(img.reshape(-1, 3).astype(np.float32) * self._channel_boost, 0, 255)
            offset_fn = ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
            if offset_fn is not None:
                w, h = self.frame_target_size
                offset = offset_fn(h, w, self._dither_strength)
                flat = np.clip(flat + offset.reshape(-1, 1), 0, 255)
            # Single distance matrix (with gray penalty, scaled to the active
            # metric) shared across all downstream decisions — per-pixel argmin,
            # the bg picker, and the per-cell fg search all need to agree on which
            # palette entry "wins" for a given pixel, so apply the bias once at
            # the top. In-place add avoids a second ~256 KB allocation each frame.
            all_d = quantize_distances_for(flat, perceptual=self._perceptual)  # (4000, 16)
            all_d += self._gray_penalty * self._penalty_scale
        per_pixel = np.argmin(all_d, axis=1)
        if self._fixed_bg is not None:
            bg = self._fixed_bg
        else:
            smoothed = ema_counts(self, per_pixel)
            if self.palette_mode == "vivid":
                picks = pick_diverse_top_n(smoothed, 3)
            else:
                picks = [int(x) for x in np.argsort(smoothed)[-3:]]
            bg = np.array(sorted(picks), dtype=np.int64)  # (3,)

        # Group per-pixel distances into 1000 cells of 4 pixels each.
        d_grid = (
            all_d.reshape(50, 80, 16)
            .reshape(25, 2, 40, 2, 16)
            .transpose(0, 2, 1, 3, 4)
            .reshape(1000, 4, 16)
        )

        bg_d = d_grid[:, :, bg]  # (1000, 4, 3)
        fg_d = d_grid[:, :, :8]  # (1000, 4, 8)

        # For each (cell, pixel, fg_candidate), pick the best of {bg0,bg1,bg2,fg}.
        # The best-bg choice is fg-independent — collapse it first to skip the
        # (1000, 4, 8, 4) tensor the naive concat+argmin would build.
        bg_argmin = bg_d.argmin(axis=2)  # (1000, 4)  -> 0/1/2
        bg_min = bg_d.min(axis=2)[:, :, None]  # (1000, 4, 1)
        minv = np.minimum(fg_d, bg_min)  # (1000, 4, 8)
        err_per_fg = minv.sum(axis=1)  # (1000, 8)
        best_fg = err_per_fg.argmin(axis=1)  # (1000,)

        force_palette_active = self._force_palette and self._color_map is not None
        if self._dither_method in ("floyd-steinberg", "atkinson") and not force_palette_active:
            # Re-dither each cell's own 2×2 pixels against its resolved
            # candidate set {bg0, bg1, bg2, fg} — candidate SELECTION (bg,
            # best_fg above) stays on the EMA-smoothed histogram for temporal
            # stability; only the per-pixel fill dithers. Candidate order
            # matches the fa code convention (0/1/2 = bg slot, 3 = fg), so the
            # returned code IS fa directly.
            pixels_cell = (
                flat.reshape(50, 80, 3)
                .reshape(25, 2, 40, 2, 3)
                .transpose(0, 2, 1, 3, 4)
                .reshape(1000, 2, 2, 3)
            )
            cand_bgr = np.concatenate(
                [
                    np.broadcast_to(C64_PALETTE_BGR[bg], (1000, 3, 3)),
                    C64_PALETTE_BGR[best_fg][:, None, :],
                ],
                axis=1,
            )  # (1000, 4, 3)
            fa = error_diffuse_cells(
                pixels_cell, cand_bgr, self._dither_method, self._dither_strength
            ).reshape(1000, 4)
        else:
            idx = np.arange(1000)
            fg_wins = fg_d[idx, :, best_fg] < bg_min[:, :, 0]  # (1000, 4)
            fa = np.where(fg_wins, 3, bg_argmin).astype(np.int64)  # (1000, 4)

        screen = ((fa[:, 0] << 6) | (fa[:, 1] << 4) | (fa[:, 2] << 2) | fa[:, 3]).astype(np.uint8)
        color = (best_fg + 8).astype(np.uint8)  # high bit = multicolor

        # text surface present for the buffers contract; MCM rejects PETSCII
        # text overlays (color-RAM bit 3 = multicolor), so nothing paints it.
        return {"screen": screen, "color": color, "bg": bg, "text": CharTextSurface(screen, color)}

    def push(self, api: C64Backend, buffers: MCMComposeBuffers) -> None:
        bg = buffers["bg"]
        if self._last_bg is None or not np.array_equal(bg, self._last_bg):
            # D020-D023 are contiguous: border, bg0, bg1, bg2.
            api.write_regs("d020", int(bg[0]), int(bg[0]), int(bg[1]), int(bg[2]))
            self._last_bg = bg.copy()
        api.write_region(0x0400, buffers["screen"].tobytes(), region_id=RegionID.SCREEN)
        api.write_region(0xD800, buffers["color"].tobytes(), region_id=RegionID.COLOR)

    def apply_fade(self, buffers: MCMComposeBuffers) -> MCMComposeBuffers:
        """MCM colors live in three places: the shared bg0/bg1/bg2 registers
        (`bg`, any palette index) and the per-cell multicolor foreground stored
        in color RAM as ``fg | 8`` with ``fg`` ∈ 0..7. Dim all four; the screen
        buffer is 2-bit selectors among them, so it's left untouched. The
        foreground uses a 0..7-constrained LUT so the dimmed value stays a legal
        multicolor color (and the bit-3 flag is preserved)."""
        alpha = self._fade_lut_alpha
        lut = build_fade_lut(alpha)
        fg_lut = build_fade_lut(alpha, allowed=tuple(range(8)))
        out: MCMComposeBuffers = dict(buffers)  # type: ignore[assignment]
        color = buffers["color"]
        out["color"] = (fg_lut[color & 0x07] | 0x08).astype(np.uint8)
        out["bg"] = lut[buffers["bg"]]
        return out
