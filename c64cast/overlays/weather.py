"""Current weather conditions in a screen corner.

Two providers:
  open-meteo  — needs lat + lon; free, no key. Returns temperature and
                a numeric weather code we map to a short label.
  wttr.in     — needs `location`; free, plain text. Returns a "%t %C"
                string we lightly post-process.

A background thread polls every `refresh_minutes` (default 10). The
render loop reads whatever the latest cache says; transient API failures
keep the last good value visible.
"""
from __future__ import annotations

import logging
import re
import threading

import requests

from .._pollthread import PollThread
from . import register
from .corner_text import CornerTextOverlay

log = logging.getLogger(__name__)

# Compact 5-char weather labels by WMO code (Open-Meteo).
# Anything missing → "MIST" (the catch-all for "atmospheric weirdness").
WMO_CODES = {
    0: "CLEAR", 1: "FAIR ", 2: "PCLDY", 3: "CLOUD",
    45: "FOG  ", 48: "FOG  ",
    51: "DRIZL", 53: "DRIZL", 55: "DRIZL",
    56: "FRZRN", 57: "FRZRN",
    61: "RAIN ", 63: "RAIN ", 65: "RAIN ",
    66: "FRZRN", 67: "FRZRN",
    71: "SNOW ", 73: "SNOW ", 75: "SNOW ", 77: "SLEET",
    80: "SHOWR", 81: "SHOWR", 82: "SHOWR",
    85: "SNOW ", 86: "SNOW ",
    95: "STORM", 96: "STORM", 99: "STORM",
}


def _fetch_open_meteo(lat: float, lon: float, units: str) -> str:
    """One-shot. Returns a short string like '72F CLEAR' or '' on failure."""
    unit_param = "fahrenheit" if units.upper() == "F" else "celsius"
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&current=temperature_2m,weather_code"
           f"&temperature_unit={unit_param}")
    r = requests.get(url, timeout=4.0)
    r.raise_for_status()
    data = r.json()
    cur = data.get("current", {})
    t = cur.get("temperature_2m")
    code = cur.get("weather_code")
    if t is None:
        return ""
    label = WMO_CODES.get(int(code) if code is not None else -1, "MIST ")
    return f"{int(round(t))}{units.upper()} {label.strip()}"


def _fetch_wttr(location: str) -> str:
    """Returns the response trimmed. wttr.in's %t includes the degree sign;
    we strip it to keep the string ASCII-clean for screen-code conversion."""
    url = f"https://wttr.in/{location}?format=%t+%C"
    r = requests.get(url, timeout=4.0)
    r.raise_for_status()
    raw = r.text.strip()
    # %t looks like "+72°F" — strip leading +, replace °.
    raw = raw.lstrip("+").replace("°", "")
    # Compress whitespace and clip; screen-code conversion only handles ASCII.
    raw = re.sub(r"[^A-Za-z0-9 \-]", "", raw).upper()
    return raw[:12]


@register("weather")
class WeatherOverlay(CornerTextOverlay):
    HELP = "Temperature + conditions in a corner (background poll)."
    PARAM_HELP = {
        "provider": "Weather source: 'open-meteo' or 'wttr.in'.",
        "lat": "Latitude (open-meteo; with lon).",
        "lon": "Longitude (open-meteo; with lat).",
        "location": "Location name (wttr.in; alternative to lat/lon).",
        "units": "Temperature units: 'F' or 'C'.",
        "refresh_minutes": "Minutes between background weather polls.",
    }

    def __init__(self, provider: str = "open-meteo",
                 lat: float | None = None,
                 lon: float | None = None,
                 location: str | None = None,
                 units: str = "F",
                 corner: str = "top-left",
                 fg_color: str = "light blue",
                 bg_color: str = "black",
                 refresh_minutes: float = 10.0):
        if provider not in ("open-meteo", "wttr.in"):
            raise ValueError(
                f"weather: provider must be 'open-meteo' or 'wttr.in', "
                f"got {provider!r}")
        if provider == "open-meteo" and (lat is None or lon is None):
            raise ValueError("weather: open-meteo requires lat + lon")
        if provider == "wttr.in" and not location:
            raise ValueError("weather: wttr.in requires location")
        if units.upper() not in ("F", "C"):
            raise ValueError("weather: units must be 'F' or 'C'")
        # Render-side refresh fast (cheap change-detection); the actual API
        # poll honors refresh_minutes via the background thread.
        super().__init__(corner=corner, fg_color=fg_color, bg_color=bg_color,
                         refresh_s=1.0)
        self.provider = provider
        self.lat = lat
        self.lon = lon
        self.location = location
        self.units = units.upper()
        self.poll_interval_s = max(60.0, float(refresh_minutes) * 60.0)
        self._cached = "--"
        self._lock = threading.Lock()
        # First fetch runs immediately so the user isn't stuck on "--" for
        # the whole first interval; subsequent fetches honor the cadence.
        self._poll = PollThread(self._poll_once, period=self.poll_interval_s,
                                name="weather-poll")

    def _fetch_once(self) -> str:
        try:
            if self.provider == "open-meteo":
                assert self.lat is not None and self.lon is not None
                return _fetch_open_meteo(self.lat, self.lon, self.units)
            assert self.location is not None
            return _fetch_wttr(self.location)
        except requests.RequestException as e:
            log.info("weather: fetch failed (%s); keeping cached value", e)
            return ""
        except Exception:
            log.exception("weather: unexpected fetch error")
            return ""

    def _poll_once(self) -> None:
        result = self._fetch_once()
        if result:
            with self._lock:
                self._cached = result

    def setup(self, api, scene):
        super().setup(api, scene)
        self._poll.start()

    def compute_strings(self, t: float) -> list[str] | None:
        with self._lock:
            return [self._cached]

    def teardown(self, api, scene):
        self._poll.stop()
        super().teardown(api, scene)
