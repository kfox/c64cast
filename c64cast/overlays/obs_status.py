"""OBS Studio status overlay — current scene + dropped-frames count.

Polls an OBS instance via obs-websocket v5 in a background thread,
displays a short string in a screen corner. Designed for the
streaming-director use case: at a glance you can see which OBS scene
is live and whether the encoder is losing frames.

Requires the ``obs`` extra (``pip install c64cast[obs]``). If
obsws-python is not installed the overlay raises with a clear message
at construction time rather than at the first frame.

OBS exposes the websocket on ws://<host>:4455 by default; set a password
in OBS → Tools → WebSocket Server Settings if you want auth.
"""

from __future__ import annotations

import logging
import threading

from .._pollthread import PollThread
from . import register
from .corner_text import CornerTextOverlay

log = logging.getLogger(__name__)

try:
    import obsws_python as _obsws

    OBSWS_AVAILABLE = True
except ImportError:
    _obsws = None
    OBSWS_AVAILABLE = False


@register("obs_status")
class OBSStatusOverlay(CornerTextOverlay):
    """Show current OBS scene + dropped-frames count in a screen corner.

    Polls the OBS websocket every `refresh_s` seconds from a background
    thread; the foreground render reads the cached string so a slow OBS
    response can't block the playlist. If the connection drops the
    overlay shows ``OBS OFFLINE`` until it reconnects."""

    HELP = "OBS Studio current scene + dropped-frame count (OBS WebSocket)."
    PARAM_HELP = {
        "host": "OBS WebSocket host.",
        "port": "OBS WebSocket port.",
        "password": "OBS WebSocket password (if auth is enabled).",
        "show_dropped": "Append the dropped-frame count to the status line.",
    }

    def __init__(
        self,
        host: str = "localhost",
        port: int = 4455,
        password: str = "",
        show_dropped: bool = True,
        corner: str = "bottom-right",
        fg_color: str = "light green",
        bg_color: str = "black",
        refresh_s: float = 2.0,
    ):
        if not OBSWS_AVAILABLE:
            raise RuntimeError(
                "obs_status overlay requires obsws-python (pip install c64cast[obs])"
            )
        # CornerTextOverlay handles paint-throttling internally; we set a
        # short refresh_s here so the corner-text base renders immediately
        # whenever our cached string changes (the OBS poll runs at a
        # separate cadence in the background thread).
        super().__init__(corner=corner, fg_color=fg_color, bg_color=bg_color, refresh_s=0.5)
        self.host = host
        self.port = int(port)
        self.password = password
        self.show_dropped = bool(show_dropped)
        self.poll_interval = float(refresh_s)
        self._lines: list[str] = ["OBS …"]
        self._lines_lock = threading.Lock()
        self._poll = PollThread(self._worker, name="obs-status", manual=True, join_timeout=1.0)
        self._client = None

    # ---- background polling --------------------------------------------------
    def _connect(self):
        assert _obsws is not None
        return _obsws.ReqClient(
            host=self.host,
            port=self.port,
            password=self.password or None,
            timeout=2.0,
        )

    def _poll_once(self) -> list[str]:
        if self._client is None:
            self._client = self._connect()
        scene_resp = self._client.get_current_program_scene()
        scene = getattr(scene_resp, "current_program_scene_name", None) or getattr(
            scene_resp, "scene_name", "?"
        )
        lines = [str(scene).upper()[:14]]
        if self.show_dropped:
            stats = self._client.get_stats()
            dropped = (getattr(stats, "output_skipped_frames", 0) or 0) + (
                getattr(stats, "render_skipped_frames", 0) or 0
            )
            lines.append(f"DROP {int(dropped):>4}")
        return lines

    def _worker(self, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                lines = self._poll_once()
                backoff = 1.0
                with self._lines_lock:
                    self._lines = lines
            except Exception as e:
                log.debug("OBS poll failed: %s", e)
                with self._lines_lock:
                    self._lines = ["OBS OFFLINE"]
                # Drop the stale client; reconnect next iteration.
                self._client = None
                backoff = min(backoff * 2.0, 30.0)
            stop.wait(timeout=max(self.poll_interval, backoff))

    # ---- overlay surface -----------------------------------------------------
    def setup(self, api, scene):
        self._poll.start()

    def compute_strings(self, t: float) -> list[str] | None:
        with self._lines_lock:
            return list(self._lines)

    def teardown(self, api, scene):
        self._poll.stop()
        if self._client is not None:
            try:
                # obsws-python's ReqClient has a .disconnect() method
                # under both v1.x and v2.x APIs.
                close = getattr(self._client, "disconnect", None)
                if close is None:
                    close = getattr(self._client, "close", None)
                if close is not None:
                    close()
            except Exception:
                log.debug("OBS disconnect failed", exc_info=True)
            self._client = None
        super().teardown(api, scene)
