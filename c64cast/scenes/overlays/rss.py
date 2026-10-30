"""RSS/Atom ticker — fetches a feed in a background thread, joins the
latest N item titles with a separator, and scrolls them across a row
via the marquee mechanism.

Uses stdlib `xml.etree.ElementTree` so there's no new dependency. Handles
both RSS 2.0 (<item><title>) and Atom (<entry><title>) layouts.
"""

from __future__ import annotations

import logging
import re
import threading
import xml.etree.ElementTree as ET

import requests

from c64cast._pollthread import PollThread

from . import _fetch, register
from .marquee import MarqueeBase

log = logging.getLogger(__name__)

# Atom tags carry an XML namespace and RSS 2.0's do not; stripping it lets one
# match serve both.
_NS_STRIP = re.compile(r"\{[^}]*\}")


def _local(tag: str) -> str:
    return _NS_STRIP.sub("", tag)


_XML_DECLARED_ENCODING = re.compile(rb"""<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z][\w.-]*)["']""")


def _parse_feed(xml: bytes) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except LookupError:
        # An encoding name Python has no codec for (x-sjis, x-euc-jp). expat
        # ignores the declaration on str input, as it did when feeds were
        # parsed from decoded text.
        return ET.fromstring(xml.decode("utf-8", errors="replace"))
    except ValueError:
        # expat decodes only UTF-8, UTF-16 and single-byte encodings, and
        # refuses a declared multi-byte one (Shift_JIS, GB2312, Big5) this way.
        declared = _XML_DECLARED_ENCODING.match(xml)
        if declared is None:
            raise
        return ET.fromstring(xml.decode(declared[1].decode("ascii"), errors="replace"))


def _extract_titles(xml_text: bytes, limit: int) -> list[str]:
    root = _parse_feed(xml_text)
    titles = []
    # 'item' is RSS, 'entry' is Atom.
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        for child in el:
            if _local(child.tag) == "title":
                text = (child.text or "").strip()
                if text:
                    titles.append(text)
                break
        if len(titles) >= limit:
            break
    return titles


@register("rss")
class RssOverlay(MarqueeBase):
    HELP = "Ticker fed by a background RSS/Atom feed fetch."
    PARAM_HELP = {
        "url": "RSS/Atom feed URL to fetch.",
        "max_items": "Maximum number of headlines to include in the ticker.",
        "refresh_minutes": "Minutes between background feed fetches.",
        "separator": "Text placed between consecutive headlines.",
    }

    def __init__(
        self,
        url: str,
        row: int = 0,
        max_items: int = 10,
        refresh_minutes: float = 15.0,
        speed_cells_per_s: float = 3.0,
        separator: str = "   *   ",
        fg_color: str = "light green",
        bg_color: str = "black",
    ):
        if not url:
            raise ValueError("rss: url must be non-empty")
        super().__init__(
            row=row, speed_cells_per_s=speed_cells_per_s, fg_color=fg_color, bg_color=bg_color
        )
        self.url = url
        self.max_items = max(1, int(max_items))
        self.poll_interval_s = max(60.0, float(refresh_minutes) * 60.0)
        self.SEPARATOR = separator
        self._titles: list[str] = []
        self._lock = threading.Lock()
        self._poll = PollThread(self._fetch_once, period=self.poll_interval_s, name="rss-poll")

    def setup(self, api, scene):
        super().setup(api, scene)
        self._poll.start()

    def teardown(self, api, scene):
        self._poll.stop()

    def _fetch_once(self) -> None:
        try:
            body = _fetch.get_body(self.url, timeout=5.0, headers={"User-Agent": "c64cast/1.0"})
            titles = _extract_titles(body, self.max_items)
            if titles:
                with self._lock:
                    self._titles = titles
        except requests.RequestException as e:
            log.info("rss: fetch failed (%s); keeping cached", e)
        except _fetch.BodyLimitExceeded as e:
            log.warning("rss: feed refused (%s); keeping cached", e)
        except ET.ParseError as e:
            log.warning("rss: parse failed (%s)", e)
        except Exception:
            log.exception("rss: unexpected fetch error")

    def _current_text(self) -> str:
        with self._lock:
            titles = list(self._titles)
        if not titles:
            return "LOADING..."
        return self.SEPARATOR.join(titles).upper()
