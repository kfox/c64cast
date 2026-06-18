"""On-C64 menu overlay.

A runtime-injected `Overlay` (NOT registered for config use — the Playlist
creates one when SPACE opens the menu and removes it on close). It paints a
context-sensitive panel of the current scene's live-editable knobs on top of the
still-running scene, so each change previews immediately; on exit it can persist
changes back to the source config.

Option model (which knobs, their valid choices) comes from the introspect
metadata for the scene's type, filtered to fields tagged `apply="live"`. The
get/set wiring per field lives in `_ITEM_BUILDERS` — the part that can't be
auto-derived (it calls the display-mode live setters factored out of
`cycle_style`). New live knob = `apply="live"` in config metadata + a builder.

Rendering is mode-dispatched:
  * petscii/blank — screen codes to $0400 + color nibbles to $D800.
  * hires/mhires (single-buffer) — bitmap glyphs via `bitmap_text`.
REU-staged bitmap scenes are skipped for the panel glyphs (the live preview
still applies through the staged path); see `_render`.

Navigation is driven by the keyboard poller's nav queue (forwarded by the
Playlist as `on_key`); SPACE arrives via `on_toggle`. CRSR-down/up moves the
selection, CRSR-right/left changes the selected value (SHIFT reverses), SPACE
exits (offering a save confirmation when there are unsaved changes).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import bitmap_text, introspect
from ..c64 import KEY, SCREEN, RegionID
from . import Overlay, ascii_to_screen

if TYPE_CHECKING:
    from ..backend import C64Backend
    from ..scenes import Scene

log = logging.getLogger("c64cast.menu")

# Display modes the menu can render a panel on this cut.
_SUPPORTED_DISPLAYS = ("petscii", "blank", "hires", "mhires")
_BITMAP_DISPLAYS = ("hires", "mhires")

# Panel placement + colors (C64 palette indices).
_PANEL_TOP_ROW = 2
_PANEL_COL = 1
_PANEL_WIDTH = 38  # cells; _PANEL_COL + _PANEL_WIDTH must be <= 40
_PANEL_BG = 6  # blue backdrop (bitmap only; char mode shows the scene bg)
_FG_TITLE = 7  # yellow
_FG_TEXT = 15  # light grey
_FG_SEL = 1  # white (selected row)
_FG_DIM = 11  # dark grey (hints/header)


def can_show_menu(scene: Scene) -> bool:
    """Whether the menu can render on this scene (Phase 1 display coverage)."""
    mode = getattr(scene, "display_mode", None)
    return getattr(mode, "name", None) in _SUPPORTED_DISPLAYS


@dataclass
class MenuItem:
    """One editable knob: a label, a value kind, and get/set closures that
    read the live value and apply a change (live preview + cfg write)."""

    label: str
    kind: str  # "enum" | "int" | "float"
    get: Callable[[], Any]
    # Return value is ignored — `object` lets the builders use a one-expression
    # lambda that both writes the cfg and calls the live setter, e.g.
    # `lambda v: (setattr(cfg, ...), mode.set_palette_mode(api, v))`.
    set: Callable[[Any], object]
    choices: tuple[str, ...] = ()
    step: float = 1.0
    minimum: float = 1.0
    default_when_none: float = 30.0

    def display_value(self) -> str:
        v = self.get()
        if v is None:
            return "AUTO"
        if self.kind == "float":
            return f"{float(v):g}"
        return str(v)

    def change(self, delta: int) -> None:
        if self.kind == "enum":
            if not self.choices:
                return
            cur = self.get()
            try:
                i = self.choices.index(cur)
            except ValueError:
                i = 0
            self.set(self.choices[(i + delta) % len(self.choices)])
            return
        cur = self.get()
        base = float(cur) if cur is not None else self.default_when_none
        nv = max(self.minimum, base + delta * self.step)
        self.set(int(round(nv)) if self.kind == "int" else nv)


# --- per-field builders: introspect picks WHICH fields; these supply the live
# get/set wiring. Each returns a MenuItem or None when not applicable to the
# scene's actual display mode. ----------------------------------------------


def _build_palette_mode(scene, cfg, mode, api, fd) -> MenuItem | None:
    if mode is None or not hasattr(mode, "set_palette_mode"):
        return None
    return MenuItem(
        label="PALETTE",
        kind="enum",
        choices=fd.choices,
        get=lambda: mode.palette_mode,
        set=lambda v: (setattr(cfg, "palette_mode", v), mode.set_palette_mode(api, v)),
    )


def _build_style(scene, cfg, mode, api, fd) -> MenuItem | None:
    if mode is None or not hasattr(mode, "set_style"):
        return None
    # STYLE_NAMES (concrete styles) excludes the 'random' sentinel; fd.choices
    # includes it, so filter it out — you can't cycle to "random" live.
    choices = tuple(c for c in fd.choices if c != "random")
    return MenuItem(
        label="STYLE",
        kind="enum",
        choices=choices,
        get=lambda: mode.style,
        set=lambda v: (setattr(cfg, "style", v), mode.set_style(api, v)),
    )


def _build_duration(scene, cfg, mode, api, fd) -> MenuItem | None:
    if not hasattr(scene, "duration_s"):
        return None
    cur = getattr(scene, "duration_s", None)
    # Video scenes run until EOF (duration_s = inf); not meaningfully editable.
    if cur is None or cur == float("inf"):
        return None
    return MenuItem(
        label="DURATION",
        kind="float",
        step=5.0,
        minimum=1.0,
        get=lambda: scene.duration_s,
        set=lambda v: (setattr(cfg, "duration_s", v), setattr(scene, "duration_s", v)),
    )


def _build_target_fps(scene, cfg, mode, api, fd) -> MenuItem | None:
    if not hasattr(scene, "target_fps"):
        return None
    return MenuItem(
        label="FPS",
        kind="int",
        step=5.0,
        minimum=1.0,
        default_when_none=30.0,
        get=lambda: scene.target_fps,
        set=lambda v: (setattr(cfg, "target_fps", v), setattr(scene, "target_fps", v)),
    )


_ITEM_BUILDERS: dict[str, Callable[..., MenuItem | None]] = {
    "palette_mode": _build_palette_mode,
    "style": _build_style,
    "duration_s": _build_duration,
    "target_fps": _build_target_fps,
}


def build_menu_items(scene: Scene, api: C64Backend) -> tuple[list[str], list[MenuItem]]:
    """Build the (header lines, editable items) for a scene from introspect
    metadata (fields tagged apply="live") + the builder wiring."""
    cfg = getattr(scene, "_cfg", None)
    scene_type = getattr(cfg, "type", None)
    mode = getattr(scene, "display_mode", None)
    header = [f"SCENE: {scene_type or '?'}", f"DISPLAY: {getattr(mode, 'name', '-')}"]
    fdocs = {}
    for st in introspect.scene_types():
        if st.name == scene_type:
            fdocs = {fd.name: fd for fd in st.fields}
            break
    items: list[MenuItem] = []
    if cfg is not None:
        for name, fd in fdocs.items():
            if fd.apply != "live":
                continue
            builder = _ITEM_BUILDERS.get(name)
            if builder is None:
                continue
            item = builder(scene, cfg, mode, api, fd)
            if item is not None:
                items.append(item)
    return header, items


class MenuOverlay(Overlay):
    name = "menu"
    PAINTS_INTO_BUFFERS = False  # paints post-render, directly to RAM

    def __init__(
        self,
        scene: Scene,
        api: C64Backend,
        *,
        can_save: bool,
        prompt_to_save: bool,
        save_fn: Callable[[], bool],
        logger: logging.Logger | None = None,
    ) -> None:
        self.scene = scene
        self.header, self.items = build_menu_items(scene, api)
        self.sel = 0
        self.state = "browse"  # "browse" | "confirm"
        self.dirty = False
        self.closed = False
        self.can_save = can_save
        self.prompt_to_save = prompt_to_save
        self.save_fn = save_fn
        self.log = logger or log
        mode = getattr(scene, "display_mode", None)
        self._is_bitmap = getattr(mode, "name", None) in _BITMAP_DISPLAYS
        self._staged = bool(getattr(mode, "use_reu_staged", False))
        self._glyphs = bitmap_text.load_glyphs() if self._is_bitmap else None
        self._warned_staged = False

    # --- input -------------------------------------------------------------

    def on_key(self, code: int, shift: bool) -> None:
        """Handle one nav key (forwarded from the poller's nav queue)."""
        if self.state == "confirm":
            if code == KEY.RETURN:
                ok = self.save_fn()
                self.log.info("menu: config saved" if ok else "menu: save failed")
            else:
                self.log.info("menu: closed without saving")
            self.closed = True
            return
        if not self.items:
            return
        delta = -1 if shift else 1
        if code == KEY.CRSR_DOWN:
            self.sel = (self.sel + delta) % len(self.items)
        elif code == KEY.CRSR_RIGHT:
            self.items[self.sel].change(delta)
            self.dirty = True

    def on_toggle(self) -> bool:
        """Handle SPACE. Returns True if the menu is now closed."""
        if self.state == "confirm":
            self.log.info("menu: closed without saving")
            self.closed = True
            return True
        return self._begin_close()

    def _begin_close(self) -> bool:
        if self.dirty and self.can_save and self.prompt_to_save:
            self.state = "confirm"
            return False
        if self.dirty and not self.can_save:
            self.log.info("menu: changes applied to the running scene (save unavailable)")
        self.closed = True
        return True

    # --- rendering ---------------------------------------------------------

    def _panel_lines(self) -> list[tuple[str, int]]:
        """(text padded to width, fg index) for each panel row."""
        rows: list[tuple[str, int]] = []

        def row(text: str, fg: int) -> None:
            rows.append((text[:_PANEL_WIDTH].ljust(_PANEL_WIDTH), fg))

        row("== C64CAST MENU ==", _FG_TITLE)
        for h in self.header:
            row(h, _FG_DIM)
        row("", _FG_DIM)
        if self.state == "confirm":
            row("SAVE CHANGES TO CONFIG?", _FG_TITLE)
            row("RETURN = YES   SPACE = NO", _FG_TEXT)
            return rows
        if not self.items:
            row("NO LIVE OPTIONS FOR THIS SCENE", _FG_TEXT)
            row("SPACE = CLOSE", _FG_DIM)
            return rows
        for i, it in enumerate(self.items):
            cursor = ">" if i == self.sel else " "
            fg = _FG_SEL if i == self.sel else _FG_TEXT
            row(f"{cursor}{it.label}: {it.display_value()}", fg)
        row("", _FG_DIM)
        row("CRSR: MOVE / CHANGE   SPACE: EXIT", _FG_DIM)
        return rows

    def process_frame(self, api: C64Backend, scene: Scene, t: float) -> None:
        self._render(api)

    def _render(self, api: C64Backend) -> None:
        if self._is_bitmap and self._staged:
            if not self._warned_staged:
                self.log.warning(
                    "menu: panel not drawn on REU-staged bitmap scenes "
                    "(set use_reu_staged=false to see it); live preview still active"
                )
                self._warned_staged = True
            return
        for idx, (text, fg) in enumerate(self._panel_lines()):
            cell_row = _PANEL_TOP_ROW + idx
            if cell_row >= SCREEN.H_CHARS:
                break
            if self._is_bitmap:
                self._paint_bitmap_row(api, cell_row, idx, text, fg)
            else:
                self._paint_char_row(api, cell_row, idx, text, fg)

    def _paint_char_row(self, api: C64Backend, cell_row: int, idx: int, text: str, fg: int) -> None:
        screen = bytes(ascii_to_screen(text))
        color = bytes([fg & 0x0F] * len(text))
        base = cell_row * SCREEN.W_CHARS + _PANEL_COL
        api.write_region(SCREEN.RAM + base, screen, region_id=RegionID.MENU_ROW_SCREEN + idx)
        api.write_region(SCREEN.COLOR_RAM + base, color, region_id=RegionID.MENU_ROW_COLOR + idx)

    def _paint_bitmap_row(
        self, api: C64Backend, cell_row: int, idx: int, text: str, fg: int
    ) -> None:
        assert self._glyphs is not None
        bitmap_text.paint_text_row(
            api,
            self._glyphs,
            cell_row=cell_row,
            text=text,
            fg=fg,
            bg=_PANEL_BG,
            col=_PANEL_COL,
            bitmap_base=SCREEN.BITMAP,
            screen_base=SCREEN.RAM,
            bitmap_region_id=RegionID.MENU_ROW_BITMAP + idx,
            screen_region_id=RegionID.MENU_ROW_SCREEN + idx,
        )
