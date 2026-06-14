"""Network status in a screen corner.

Configurable list of items to show, in order, one per line:
  ip       — this machine's outbound-facing IP (the one used to reach the U64).
  hostname — `socket.gethostname()`.
  ping     — TCP-connect latency to the U64's HTTP port, ms.

Polled by a background thread at `refresh_s` seconds so the render loop
doesn't block on socket / DNS / connect. Failures keep the previous value.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import urlparse

from .._pollthread import PollThread
from . import register
from .corner_text import CornerTextOverlay

log = logging.getLogger(__name__)

VALID_ITEMS = ("ip", "hostname", "ping")


def _outbound_ip(target_host: str) -> str:
    """Return the local IP used to reach `target_host`. UDP connect is the
    classic trick — no packet is actually sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_host, 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def _tcp_ping_ms(host: str, port: int, timeout: float = 1.0) -> float:
    t0 = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return (time.monotonic() - t0) * 1000.0
    finally:
        sock.close()


@register("network")
class NetworkOverlay(CornerTextOverlay):
    HELP = "Local IP / hostname / U64 ping latency in a corner."
    PARAM_HELP = {
        "items": "Which lines to show, any of: 'ip', 'hostname', 'ping'.",
    }

    def __init__(
        self,
        items: list | None = None,
        corner: str = "bottom-right",
        fg_color: str = "light gray",
        bg_color: str = "black",
        refresh_s: float = 5.0,
    ):
        items = items or ["ip", "ping"]
        bad = [i for i in items if i not in VALID_ITEMS]
        if bad:
            raise ValueError(f"network: unknown items {bad!r} (known: {', '.join(VALID_ITEMS)})")
        super().__init__(corner=corner, fg_color=fg_color, bg_color=bg_color, refresh_s=1.0)
        self.items = list(items)
        self.poll_interval_s = max(1.0, float(refresh_s))
        self._cached_lines: list[str] = ["..." for _ in items]
        self._lock = threading.Lock()
        self._target_host: str | None = None
        self._target_port: int = 80
        self._poll = PollThread(self._poll_once, period=self.poll_interval_s, name="network-poll")

    def setup(self, api, scene):
        super().setup(api, scene)
        # Parse the U64 URL once so we know what to ping. The api object
        # is the scene's, not ours — derive from base_url. Backends without
        # a REST base URL (e.g. TeensyROM) leave the ping target unset; the
        # "ping" item then degrades to a dash instead of crashing setup.
        base_url = getattr(api, "base_url", None)
        if base_url:
            parsed = urlparse(base_url)
            self._target_host = parsed.hostname or "localhost"
            self._target_port = parsed.port or 80
        else:
            self._target_host = None
            self._target_port = 80
        self._poll.start()

    def teardown(self, api, scene):
        self._poll.stop()
        super().teardown(api, scene)

    def _poll_once(self) -> None:
        new_lines = self._collect()
        with self._lock:
            self._cached_lines = new_lines

    def _collect(self) -> list[str]:
        out = []
        for item in self.items:
            try:
                if item == "ip":
                    out.append(_outbound_ip(self._target_host or "8.8.8.8"))
                elif item == "hostname":
                    out.append(socket.gethostname().upper()[:14])
                elif item == "ping":
                    ms = _tcp_ping_ms(self._target_host or "", self._target_port)
                    out.append(f"PING {ms:>3.0f}MS")
            except Exception as e:
                log.debug("network %s failed: %s", item, e)
                # Preserve the last good value rather than clobbering with "?".
                with self._lock:
                    prev = (
                        self._cached_lines[len(out)] if len(out) < len(self._cached_lines) else "?"
                    )
                out.append(prev)
        return out

    def compute_strings(self, t: float):
        with self._lock:
            return list(self._cached_lines)
