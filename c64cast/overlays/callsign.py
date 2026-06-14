"""Static text in a screen corner. Intended for ham-radio callsigns,
booth IDs, sponsor tags, etc. — any short string that doesn't change."""

from __future__ import annotations

from . import register
from .corner_text import CornerTextOverlay


@register("callsign")
class CallsignOverlay(CornerTextOverlay):
    HELP = "Static, unchanging text in a corner (callsign, booth ID, sponsor tag)."
    PARAM_HELP = {"text": "The fixed string to display."}

    def __init__(
        self,
        text: str = "",
        corner: str = "bottom-right",
        fg_color: str = "white",
        bg_color: str = "black",
    ):
        if not text:
            raise ValueError("callsign: text must be non-empty")
        # Static text → render once, never again. Big refresh interval +
        # the change-detect in the base means after the first paint there's
        # zero traffic until teardown.
        super().__init__(corner=corner, fg_color=fg_color, bg_color=bg_color, refresh_s=86400.0)
        self.text = str(text)

    def compute_strings(self, t: float) -> list[str] | None:
        return [self.text]
