"""Present c64cast as a virtual WLED device (WLED bridge Mode 1).

Where Mode 3 (``wled_sync.py``) pushes SID features *out* as WLED Audio Sync
packets, Mode 1 is the other direction: c64cast advertises itself on the LAN as
a WLED device (mDNS ``_wled._tcp``) and serves a subset of WLED's JSON HTTP/WS
API, so the WLED mobile app / `python-wled` / Home Assistant can **discover and
control** it with no c64cast-specific client.

The mapping (WLED's control model → c64cast):

* **effects list ↔ scenes** — the WLED "effect" dropdown lists the playlist's
  scene names; selecting one (``seg[].fx``) jumps that system's playlist to it.
* **on / brightness ↔ transport** — ``on=false`` or ``bri=0`` pauses; ``on=true``
  with ``bri>0`` resumes. The brightness value is stored and echoed so the app UI
  round-trips (live pixel-dimming of the C64 output is a follow-up).
* **speed / intensity sliders ↔ live params** — ``sx``/``ix`` drive the current
  scene's declared ``LIVE_PARAMS`` (the same seam `midi_control` sweeps), so a
  generator's speed/scale respond to the app's sliders. No-op when the current
  scene declares no matching param.

**Ensemble = one WLED segment per system.** Segment *i* maps to the *i*-th
system (ensemble order); a single-system run is one segment. Top-level ``on`` /
``bri`` apply to every system at once (WLED's master switch); per-segment fields
target that one system. The WLED effect list is shared (WLED has one global
effect array): it's built from the first system's scenes and a segment's ``fx``
indexes into it, clamped to that system's own scene count.

Runs the FastAPI app on the shared uvicorn wrapper (`control_plane.ControlServer`)
and registers an mDNS service via ``zeroconf``. Needs the ``wled`` extra
(zeroconf + fastapi + uvicorn); a graceful ``RuntimeError`` names the missing
piece, mirroring the control-plane pattern.
"""

import hashlib
import logging
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from .playlist import Playlist

# NOTE: this module deliberately does NOT use `from __future__ import
# annotations`. The FastAPI route handlers below annotate params with types
# imported *inside* build_wled_app (Request / WebSocket); with stringized
# annotations FastAPI can't resolve those local names from module globals and
# would mis-read `request` as a query param and skip WebSocket injection. Real
# (eagerly-evaluated) annotations resolve against the enclosing function scope.

log = logging.getLogger(__name__)

WLED_SERVICE_TYPE = "_wled._tcp.local."

# WLED sliders (bri / sx / ix / col channels) are 0..255.
_SLIDER_MAX = 255.0

# WLED info payload cosmetics — enough for real clients (app / python-wled /
# Home Assistant) to parse us as a WLED device. We pin a plausible firmware
# version and identify the product as c64cast.
_WLED_VERSION = "0.14.0"
_WLED_VID = 2405120  # a WLED build-date "version id"; clients only compare it

# Which of the current scene's LIVE_PARAMS the WLED speed / intensity sliders
# drive, in priority order — the first one the scene declares wins. Kept small
# and predictable (the common generative knobs); extend as more scenes expose
# params. A slider with no matching param on the current scene is a silent no-op.
_SX_TARGETS = ("source.speed", "source.scroll_speed", "effect.decay")

# Real WLED serves its own control UI at "/" (a full SPA). Several third-party
# WLED companion apps (macOS/iOS "shell" apps in particular) don't reimplement
# controls natively — they open a WebView pointed at the device's own "/" and
# render whatever comes back. With no route there, FastAPI's default 404 body
# renders as literal on-screen text and every control (power aside) is
# invisible in that app, even though fx/sx/ix are functionally wired via
# /json/state. This is a small hand-rolled page (fetch-driven against our own
# /json endpoints) covering exactly what the backend currently acts on —
# transport, effect select, speed/intensity — so it and any browser pointed at
# the device have something usable. No inline JS libs / CDN deps.
_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>c64cast</title>
<style>
  body { background: #111; color: #eee; font: 15px -apple-system, sans-serif;
         max-width: 480px; margin: 2em auto; padding: 0 1em; }
  h1 { font-size: 1.3em; }
  h2 { font-size: 1em; color: #aaa; margin-top: 2em; }
  .row { display: flex; align-items: center; gap: 0.75em; margin: 0.6em 0; }
  .row label { width: 90px; flex-shrink: 0; color: #ccc; }
  input[type=range] { flex: 1; }
  select { flex: 1; background: #222; color: #eee; border: 1px solid #444;
           padding: 0.3em; }
  .segment { border-top: 1px solid #333; padding-top: 0.5em; }
</style>
</head>
<body>
<h1>c64cast <small style="color:#777">(WLED control surface)</small></h1>
<div class="row">
  <label for="on">Power</label>
  <input type="checkbox" id="on">
</div>
<div class="row">
  <label for="bri">Brightness</label>
  <input type="range" id="bri" min="0" max="255">
</div>
<div id="segments"></div>
<script>
async function post(body) {
  await fetch('/json/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
}

function segmentEl(i, seg, effects) {
  const wrap = document.createElement('div');
  wrap.className = 'segment';
  wrap.innerHTML = '<h2>System ' + (i + 1) + '</h2>';

  const fxRow = document.createElement('div');
  fxRow.className = 'row';
  const fxLabel = document.createElement('label');
  fxLabel.textContent = 'Scene';
  const fxSel = document.createElement('select');
  effects.forEach((name, idx) => {
    const opt = document.createElement('option');
    opt.value = idx;
    opt.textContent = name;
    if (idx === seg.fx) opt.selected = true;
    fxSel.appendChild(opt);
  });
  fxSel.onchange = () => post({seg: [{id: i, fx: parseInt(fxSel.value, 10)}]});
  fxRow.appendChild(fxLabel);
  fxRow.appendChild(fxSel);
  wrap.appendChild(fxRow);

  [['Speed', 'sx', seg.sx], ['Intensity', 'ix', seg.ix]].forEach(([label, key, val]) => {
    const row = document.createElement('div');
    row.className = 'row';
    const l = document.createElement('label');
    l.textContent = label;
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = 0;
    slider.max = 255;
    slider.value = val;
    slider.oninput = () => {
      const body = {seg: [{id: i}]};
      body.seg[0][key] = parseInt(slider.value, 10);
      post(body);
    };
    row.appendChild(l);
    row.appendChild(slider);
    wrap.appendChild(row);
  });
  return wrap;
}

async function refresh() {
  const active = document.activeElement;
  if (active && (active.tagName === 'INPUT' || active.tagName === 'SELECT')) return;
  const r = await fetch('/json');
  const d = await r.json();
  document.getElementById('on').checked = d.state.on;
  document.getElementById('bri').value = d.state.bri;
  const segsEl = document.getElementById('segments');
  segsEl.innerHTML = '';
  d.state.seg.forEach((seg, i) => segsEl.appendChild(segmentEl(i, seg, d.effects)));
}

document.getElementById('on').onchange = (e) => post({on: e.target.checked});
document.getElementById('bri').oninput = (e) => post({bri: parseInt(e.target.value, 10)});

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""
_IX_TARGETS = ("source.scale", "source.intensity")


def _local_ip() -> str:
    """Best-effort primary LAN IPv4 for the mDNS A record. Uses a UDP connect
    trick (no packets are actually sent) so it picks the interface the OS would
    route LAN traffic over, not loopback. Falls back to 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, guaranteed unroutable off-LAN
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _set_live_param(pl: Playlist, targets: tuple[str, ...], value_0_255: int) -> None:
    """Drive the current scene's first-declared LIVE_PARAM among `targets` from a
    0..255 WLED slider value. Mirrors midi_control._apply_param's holder/LIVE_PARAMS
    lookup; a silent no-op when no scene / no matching param (documented)."""
    scene = pl.current
    if scene is None:
        return
    norm = max(0.0, min(1.0, value_0_255 / _SLIDER_MAX))
    for target in targets:
        holder_attr, _, name = target.partition(".")
        holder = getattr(scene, holder_attr, None)
        if holder is None:
            continue
        live_params = getattr(type(holder), "LIVE_PARAMS", {})
        if name not in live_params:
            continue
        lo, hi = live_params[name]
        setattr(holder, name, lo + norm * (hi - lo))
        return


class WledBridge:
    """Translates between WLED JSON state and the c64cast playlists.

    Holds an ordered list of (name, Playlist) — one WLED segment per system. It
    keeps a little per-segment *echo* state (brightness / palette / colors /
    slider positions) that WLED has but c64cast doesn't act on yet, so the app's
    UI round-trips; the *functional* fields (on/off, fx, sx/ix) drive the
    playlist directly.
    """

    def __init__(self, systems: list[tuple[str, Playlist]], name: str) -> None:
        if not systems:
            raise ValueError("WledBridge needs at least one system")
        self._systems = systems
        self._name = name
        # Real WLED "mac" is 12 hex digits; some clients validate/parse it as
        # such. Derive a stable pseudo-MAC from the name rather than embedding
        # non-hex characters.
        self._mac = hashlib.md5(name.encode()).hexdigest()[:12]
        self._uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"c64cast-{name}"))
        self._lock = threading.Lock()
        self._global_bri = 128
        self._transition = 7
        self._started = time.monotonic()
        # Per-segment echo state, one dict per system (indexed like _systems).
        self._seg_echo: list[dict[str, Any]] = [
            {
                "bri": 255,
                "pal": 0,
                "col": [[255, 160, 0], [0, 0, 0], [0, 0, 0]],
                "sx": 128,
                "ix": 128,
            }
            for _ in systems
        ]

    # -- reads ---------------------------------------------------------------

    def _effects(self) -> list[str]:
        """Shared WLED effect list = the first system's scene names (WLED has one
        global effect array). Never empty (WLED effect 0 must exist)."""
        scenes = self._systems[0][1].scenes
        names = [s.name for s in scenes]
        return names or ["Solid"]

    def effects(self) -> list[str]:
        return self._effects()

    def palettes(self) -> list[str]:
        # c64cast has a fixed C64 palette (no WLED-style gradient palettes yet);
        # expose a single entry so the app's palette control is well-formed.
        return ["Default"]

    def mac(self) -> str:
        return self._mac

    def device_uuid(self) -> str:
        return self._uuid

    def _seg_dict(self, i: int) -> dict[str, Any]:
        _name, pl = self._systems[i]
        echo = self._seg_echo[i]
        playing = not pl.pause_event.is_set()
        fx = pl.index if 0 <= pl.index < len(pl.scenes) else 0
        return {
            "id": i,
            "start": i,
            "stop": i + 1,
            "len": 1,
            "grp": 1,
            "spc": 0,
            "of": 0,
            "on": playing,
            "frz": False,
            "bri": echo["bri"],
            "cct": 127,
            "col": echo["col"],
            "fx": fx,
            "sx": echo["sx"],
            "ix": echo["ix"],
            "pal": echo["pal"],
            "sel": i == 0,
            "rev": False,
            "mi": False,
            "o1": False,
            "o2": False,
            "o3": False,
        }

    def state_dict(self) -> dict[str, Any]:
        with self._lock:
            any_playing = any(not pl.pause_event.is_set() for _, pl in self._systems)
            return {
                "on": any_playing,
                "bri": self._global_bri,
                "transition": self._transition,
                "ps": -1,
                "pl": -1,
                "nl": {"on": False, "dur": 60, "mode": 1, "tbri": 0, "rem": -1},
                "udpn": {"send": False, "recv": True, "sgrp": 1, "rgrp": 1},
                "lor": 0,
                "mainseg": 0,
                "seg": [self._seg_dict(i) for i in range(len(self._systems))],
            }

    def info_dict(self) -> dict[str, Any]:
        nseg = len(self._systems)
        effects = self._effects()
        return {
            "ver": _WLED_VERSION,
            "vid": _WLED_VID,
            "leds": {
                "count": nseg,
                "pwr": 0,
                "fps": 0,
                "maxpwr": 0,
                "maxseg": nseg,
                "seglc": [1] * nseg,
                "lc": 1,
                "rgbw": False,
                "wv": 0,
                "cct": 0,
            },
            "str": False,
            "name": self._name,
            "udpport": 21324,
            "live": False,
            "liveseg": -1,
            "lm": "",
            "lip": "",
            "ws": 0,
            "fxcount": len(effects),
            "palcount": len(self.palettes()),
            "cpalcount": 0,
            "maps": [{"id": 0}],
            "wifi": {"bssid": "", "rssi": -50, "signal": 100, "channel": 1},
            "fs": {"u": 0, "t": 0, "pmt": 0},
            "ndc": nseg,
            "arch": "esp32",
            "core": "c64cast",
            "lwip": 0,
            "freeheap": 200000,
            "uptime": int(time.monotonic() - self._started),
            "opt": 0,
            "brand": "WLED",
            "product": "c64cast",
            "mac": self._mac,
            "ip": _local_ip(),
        }

    def full(self) -> dict[str, Any]:
        """The `/json` payload: state + info + effects + palettes."""
        return {
            "state": self.state_dict(),
            "info": self.info_dict(),
            "effects": self.effects(),
            "palettes": self.palettes(),
        }

    # -- writes --------------------------------------------------------------

    @staticmethod
    def _set_playing(pl: Playlist, playing: bool) -> None:
        """Gate a playlist's transport (mirrors control_plane's pause/resume)."""
        if playing:
            if pl.pause_event.is_set():
                pl.resume_event.set()
        else:
            pl.pause_event.set()

    def _apply_to_system(self, i: int, seg: Mapping[str, Any], master_playing: bool) -> None:
        name, pl = self._systems[i]
        echo = self._seg_echo[i]
        # Transport: a segment is playing when master on && its own on && bri>0.
        seg_on = bool(seg.get("on", True))
        if "bri" in seg:
            echo["bri"] = max(0, min(255, int(seg["bri"])))
        playing = master_playing and seg_on and echo["bri"] > 0
        if "on" in seg or "bri" in seg:
            self._set_playing(pl, playing)
        # Effect selection → scene jump (clamped to this system's scenes).
        if "fx" in seg:
            fx = int(seg["fx"])
            if 0 <= fx < len(pl.scenes) and not pl.single_scene:
                try:
                    pl.request_jump(fx, skip_interstitial=True)
                except ValueError:
                    log.debug("wled: fx %d out of range for %s", fx, name)
        # Sliders → live params + echo.
        if "sx" in seg:
            echo["sx"] = max(0, min(255, int(seg["sx"])))
            _set_live_param(pl, _SX_TARGETS, echo["sx"])
        if "ix" in seg:
            echo["ix"] = max(0, min(255, int(seg["ix"])))
            _set_live_param(pl, _IX_TARGETS, echo["ix"])
        # Purely-echoed fields (no c64cast action yet).
        if "pal" in seg:
            echo["pal"] = int(seg["pal"])
        if "col" in seg and isinstance(seg["col"], list):
            echo["col"] = seg["col"]

    def apply(self, partial: Mapping[str, Any]) -> None:
        """Apply a partial WLED state object (from POST /json or a WS message).

        Top-level `on`/`bri`/`transition` apply to every system (WLED's master
        controls); a `seg` list targets individual systems by position/id.
        Thread-safe: playlist transport is event-based and the echo state is
        lock-guarded."""
        with self._lock:
            master_on = True
            if "transition" in partial:
                self._transition = int(partial["transition"])
            if "bri" in partial:
                self._global_bri = max(0, min(255, int(partial["bri"])))
            if "on" in partial:
                master_on = bool(partial["on"])
            master_playing = master_on and self._global_bri > 0

            segs = partial.get("seg")
            if isinstance(segs, list) and segs:
                for pos, seg in enumerate(segs):
                    if not isinstance(seg, Mapping):
                        continue
                    idx = int(seg.get("id", pos))
                    if 0 <= idx < len(self._systems):
                        self._apply_to_system(idx, seg, master_playing)
            elif "on" in partial or "bri" in partial:
                # Master on/bri with no per-segment detail → all systems.
                for i in range(len(self._systems)):
                    self._set_playing(self._systems[i][1], master_playing)


def build_wled_app(bridge: WledBridge, port: int = 80):
    """Build the FastAPI app serving the WLED JSON HTTP + WS API subset.

    `port` is echoed into /description.xml's URLBase/LOCATION (the UPnP device
    description real WLED serves for SSDP discovery) — some WLED clients fetch
    it to validate/enrich a device found via mDNS, or one added by IP, and
    silently drop/flag-offline a device that 404s on it.

    Raises RuntimeError if FastAPI isn't installed (the `wled`/`control` extra)."""
    try:
        from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
    except ImportError as e:
        raise RuntimeError("WLED device requires fastapi: pip install c64cast[wled]") from e

    app = FastAPI(title="c64cast (WLED)", version=_WLED_VERSION)
    ws_clients: set[Any] = set()

    @app.get("/")
    def index() -> Response:
        return Response(content=_INDEX_HTML, media_type="text/html")

    @app.get("/description.xml")
    def description_xml() -> Response:
        ip = _local_ip()
        name = bridge.info_dict()["name"]
        xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion>
  <major>1</major>
  <minor>0</minor>
 </specVersion>
 <URLBase>http://{ip}:{port}/</URLBase>
 <device>
  <deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
  <friendlyName>{name}</friendlyName>
  <manufacturer>WLED</manufacturer>
  <manufacturerURL>https://github.com/wled/WLED</manufacturerURL>
  <modelDescription>WLED addressable LED controller</modelDescription>
  <modelName>WLED</modelName>
  <modelNumber>{_WLED_VERSION}</modelNumber>
  <modelURL>https://github.com/wled/WLED</modelURL>
  <serialNumber>{bridge.mac()}</serialNumber>
  <UDN>uuid:{bridge.device_uuid()}</UDN>
 </device>
</root>
"""
        return Response(content=xml, media_type="text/xml")

    @app.get("/json")
    def json_full() -> dict[str, Any]:
        return bridge.full()

    @app.get("/json/state")
    def json_state() -> dict[str, Any]:
        return bridge.state_dict()

    @app.get("/json/info")
    def json_info() -> dict[str, Any]:
        return bridge.info_dict()

    @app.get("/json/si")
    def json_si() -> dict[str, Any]:
        # WLED's lightweight poll: state + info only.
        return {"state": bridge.state_dict(), "info": bridge.info_dict()}

    @app.get("/json/eff")
    def json_eff() -> list[str]:
        return bridge.effects()

    @app.get("/json/pal")
    def json_pal() -> list[str]:
        return bridge.palettes()

    async def _apply_body(body: Any) -> dict[str, Any]:
        if isinstance(body, Mapping):
            bridge.apply(body)
        return {"success": True}

    @app.post("/json")
    async def post_json(request: Request) -> dict[str, Any]:
        return await _apply_body(await request.json())

    @app.post("/json/state")
    async def post_state(request: Request) -> dict[str, Any]:
        return await _apply_body(await request.json())

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        ws_clients.add(websocket)
        # WLED sends the full state+info on connect.
        await websocket.send_json({"state": bridge.state_dict(), "info": bridge.info_dict()})
        try:
            while True:
                msg = await websocket.receive_json()
                # WLED wraps commands variously; accept a bare state object or
                # {"state": {...}} and ignore control frames we don't model.
                payload = msg.get("state", msg) if isinstance(msg, Mapping) else None
                if isinstance(payload, Mapping):
                    bridge.apply(payload)
                # Broadcast the updated state to every connected client.
                state_msg = {"state": bridge.state_dict()}
                for client in list(ws_clients):
                    try:
                        await client.send_json(state_msg)
                    except Exception:
                        ws_clients.discard(client)
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.discard(websocket)

    return app


class WledDeviceServer:
    """Runs the WLED JSON API (uvicorn, background thread) and advertises the
    device over mDNS. `start()`/`stop()` bookend the run loop, like ControlServer."""

    def __init__(self, host: str, port: int, name: str, bridge: WledBridge) -> None:
        self._host = host
        self._port = port
        self._name = name
        self._bridge = bridge
        self._app = build_wled_app(bridge, port=port)  # RuntimeError if fastapi missing
        from .control_plane import ControlServer

        self._server = ControlServer(host, port, self._app, label=f"WLED device '{name}'")
        self._zc: Any = None
        self._info: Any = None

    def _register_mdns(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError as e:
            raise RuntimeError("WLED device requires zeroconf: pip install c64cast[wled]") from e
        ip = _local_ip()
        # Advertise the real LAN IP even when bound to 0.0.0.0, so discovery
        # clients get a reachable A record + the actual port via the SRV record.
        self._zc = Zeroconf()
        self._info = ServiceInfo(
            WLED_SERVICE_TYPE,
            f"{self._name}.{WLED_SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self._port,
            properties={"md": "c64cast", "ver": _WLED_VERSION},
            server=f"{self._name.replace(' ', '-')}.local.",
        )
        self._zc.register_service(self._info)
        log.info("WLED device: advertised as %r on %s:%d (mDNS)", self._name, ip, self._port)

    def start(self) -> None:
        self._server.start()
        try:
            self._register_mdns()
        except Exception:
            # A discovery failure must not take down the (already-serving) HTTP
            # API — the device is still reachable by IP:port, just not auto-found.
            log.exception("WLED device: mDNS advertisement failed (API still serving)")

    def stop(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception:
                log.debug("WLED device: mDNS teardown hiccup", exc_info=True)
            self._zc = None
            self._info = None
        self._server.stop()


def start_wled_device(
    host: str, port: int, name: str, systems: list[tuple[str, Playlist]]
) -> WledDeviceServer:
    """Build the bridge + server, start serving, and return the handle (caller
    calls `.stop()` at shutdown). Mirrors control_plane.start_control_server."""
    bridge = WledBridge(systems, name)
    server = WledDeviceServer(host, port, name, bridge)
    server.start()
    return server
