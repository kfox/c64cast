"""Time/date overlay — short PETSCII string in a screen corner."""
from __future__ import annotations

from datetime import datetime

from . import register
from .corner_text import CornerTextOverlay


@register("clock")
class ClockOverlay(CornerTextOverlay):
    HELP = "Current time (and optional date) in a screen corner."
    PARAM_HELP = {
        "format": "strftime format for the time line (e.g. '%H:%M').",
        "show_date": "Also show a second line with the date.",
        "date_format": "strftime format for the date line when show_date is true.",
    }

    def __init__(self, corner: str = "top-right",
                 format: str = "%H:%M",
                 show_date: bool = False,
                 date_format: str = "%Y-%m-%d",
                 fg_color: str = "white",
                 bg_color: str = "black",
                 refresh_s: float = 1.0):
        super().__init__(corner=corner, fg_color=fg_color, bg_color=bg_color,
                         refresh_s=refresh_s)
        self.format = format
        self.show_date = bool(show_date)
        self.date_format = date_format

    def compute_strings(self, t: float) -> list[str] | None:
        now = datetime.now()
        lines = [now.strftime(self.format)]
        if self.show_date:
            lines.append(now.strftime(self.date_format))
        return lines
