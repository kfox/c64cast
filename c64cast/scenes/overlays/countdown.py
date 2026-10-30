"""Countdown to a target datetime, in a screen corner.

Config:
  target  — ISO 8601 string ("2026-12-31T23:59:59" or "2026-12-31 18:00").
            No UTC offset means local time; "+01:00" or "Z" pins the zone.
  format  — "auto" (default): picks the largest unit needed (D:HH:MM:SS).
            Anything else: strftime-style with placeholders {d}{h}{m}{s}.
  done_text — what to show after the target time. Default "DONE"."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import register
from .corner_text import CornerTextOverlay

log = logging.getLogger(__name__)


def _parse_target(s: str) -> datetime:
    """The target as an aware datetime; a target with no offset is local time."""
    # Tolerate the space-separated variant of ISO by swapping in a 'T'.
    try:
        target = datetime.fromisoformat(s)
    except ValueError:
        target = datetime.fromisoformat(s.replace(" ", "T"))
    return target if target.tzinfo is not None else target.astimezone()


@register("countdown")
class CountdownOverlay(CornerTextOverlay):
    HELP = "Time remaining until a target date/time, in a corner."
    PARAM_HELP = {
        "target": "Target datetime (ISO 8601, e.g. '2026-12-31T23:59').",
        "format": "'auto' for adaptive units, or a template using {d}{h}{m}{s}.",
        "done_text": "Text shown once the target has passed.",
    }

    def __init__(
        self,
        target: str,
        format: str = "auto",
        done_text: str = "DONE",
        corner: str = "bottom-left",
        fg_color: str = "yellow",
        bg_color: str = "black",
        refresh_s: float = 1.0,
    ):
        super().__init__(corner=corner, fg_color=fg_color, bg_color=bg_color, refresh_s=refresh_s)
        self.target_dt = _parse_target(target)
        self.format = format
        self.done_text = str(done_text)

    def compute_strings(self, t: float) -> list[str] | None:
        remaining = (self.target_dt - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return [self.done_text]
        total = int(remaining)
        d, rem = divmod(total, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        if self.format == "auto":
            if d > 0:
                return [f"{d}D {h:02d}:{m:02d}:{s:02d}"]
            return [f"{h:02d}:{m:02d}:{s:02d}"]
        try:
            return [self.format.format(d=d, h=h, m=m, s=s)]
        except (KeyError, IndexError, ValueError) as e:
            log.warning("countdown: bad format %r (%s)", self.format, e)
            return [f"{h:02d}:{m:02d}:{s:02d}"]
