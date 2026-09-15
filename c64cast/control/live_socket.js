/* A receive-only live-state WebSocket with a polling fallback.
 *
 * Shared by the two hand-written control pages — the `/perf` console
 * (control/perf_console.html) and the WLED bridge's device page
 * (wled/wled_index.html) — and spliced into each at render time by
 * control/page_assets.py, never served as its own file.
 */

const WS_RETRY_MIN_MS = 500;
const WS_RETRY_MAX_MS = 15000;

/* Open `path` and keep it open. Options:
 *   path      WebSocket path on this host ('/ws', '/perf/ws').
 *   onMessage called with each frame's parsed JSON; unparsable frames drop.
 *   onOpen    optional, called on every (re)connect.
 *   poll      called on an interval while the socket is down.
 *   pollMs    that interval.
 * Returns {start}. Call start() once; it re-arms itself from then on.
 */
function liveSocket({ path, onMessage, onOpen, poll, pollMs }) {
  let ws = null;
  let retryMs = 0;
  let pollTimer = null;

  function scheduleFallback() {
    if (!pollTimer) pollTimer = setInterval(poll, pollMs);
  }

  function stopFallback() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function retry() {
    retryMs = retryMs ? Math.min(retryMs * 2, WS_RETRY_MAX_MS) : WS_RETRY_MIN_MS;
    setTimeout(start, retryMs);
  }

  function start() {
    try {
      const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
      ws = new WebSocket(scheme + location.host + path);
    } catch (e) {
      scheduleFallback();
      retry();
      return;
    }
    ws.onopen = () => {
      retryMs = 0;
      stopFallback();
      if (onOpen) onOpen();
    };
    ws.onmessage = (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      onMessage(frame);
    };
    ws.onclose = () => {
      scheduleFallback();
      retry();
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch (e) {}
    };
  }

  return { start };
}
