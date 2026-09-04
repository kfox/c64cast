/* A live-state WebSocket with a polling fallback, shared by the two
 * hand-written control pages: the `/perf` console (control/perf_console.html)
 * and the WLED bridge's own device page (wled/wled_index.html).
 *
 * Both do the same thing — open a receive-only socket for state pushes, fall
 * back to polling an HTTP endpoint while it is down, and reconnect with
 * backoff — and both used to carry their own copy of it. The copies drifted:
 * different reconnect delays, and for a while neither backed off at all, so
 * every phone left open on a downed host hammered it at a fixed rate. That is
 * precisely the load `perf_console.MAX_CONSOLE_SOCKETS` exists to bound, and
 * a fix applied to one page kept missing the other.
 *
 * Spliced into both pages at render time by control/page_assets.py rather than
 * served as its own file: each page stays one self-contained document with no
 * second request and no third-party resource, which is what lets the console's
 * response headers be as strict as they are.
 *
 * The socket is receive-only in both callers — everything they send goes over
 * fetch — so nothing outside needs a handle on it.
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

  // Back off rather than retry forever at a fixed interval, and retry after a
  // construction failure too — that branch used to fall back to polling and
  // never try the socket again for the life of the page.
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
    // Only the parse is guarded. The console's own handler used to sit inside
    // this try as well, so a throw while applying a frame was swallowed; here
    // it reaches the browser console instead. Deliberate: the socket survives
    // an uncaught handler error either way, so the old shape bought a skipped
    // repaint at the price of hiding the reason for it.
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
