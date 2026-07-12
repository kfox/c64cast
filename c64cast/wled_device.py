"""Present c64cast as a virtual WLED device (WLED bridge Mode 1).

Where Mode 3 (``wled_sync.py``) pushes SID features *out* as WLED Audio Sync
packets, Mode 1 is the other direction: c64cast advertises itself on the LAN as
a WLED device (mDNS ``_wled._tcp``) and serves a subset of WLED's JSON HTTP/WS
API, so the WLED mobile app / `python-wled` / Home Assistant can **discover and
control** it with no c64cast-specific client.

The mapping (WLED's control model → c64cast):

* **effects list ↔ scenes** — the WLED "effect" dropdown lists the playlist's
  scene names; selecting one (``seg[].fx``) jumps that system's playlist to it.
* **on / brightness ↔ transport + real dim** — ``on=false`` or ``bri=0`` pauses;
  ``on=true`` with ``bri>0`` resumes. A nonzero ``bri`` is also a *real* output
  dim: the effective brightness (master ``bri`` × the segment's ``bri``, each
  0..255) is pushed onto the playlist + live display mode as ``user_dim``, which
  folds into the fade LUT so the C64 screen visibly darkens. ``bri=0`` leaves
  ``user_dim`` untouched (it's a pause), so a later power-on restores at the
  prior brightness.
* **speed / intensity sliders ↔ live params** — ``sx``/``ix`` drive the current
  scene's declared ``LIVE_PARAMS`` (the same seam `midi_control` sweeps), so a
  generator's speed/scale respond to the app's sliders. No-op when the current
  scene declares no matching param.
* **palette dropdown ↔ ``[color].palette_mode``** — the WLED palette list is the
  c64cast palette modes (percell/cheap/vivid/grayscale); selecting one (``pal``)
  live-swaps the current scene's mode via ``DisplayMode.set_palette_mode`` AND
  clears any active color-picker force (the "back to normal" path). No-op on
  scenes whose mode has no ``set_palette_mode`` (hires/petscii/blank).
* **color picker ↔ forced palette** — the up-to-3 ``col`` slots are snapped to
  their nearest C64 colors and the current scene is remapped to *only* those
  (``[color].force_palette`` posterize), live, via ``set_color_map`` + the mode's
  force toggle. Applies on mcm/mhires; echo-only elsewhere.

``pal`` and ``col`` drive the *same* C64 palette (mutually exclusive), but the
WLED app re-POSTs the full segment on every change, so each is applied only when
it changed from the last-echoed value. The WS ``/ws`` handler also pushes state
proactively on a timeout (real WLED does), so an autonomous scene change reaches
connected apps rather than leaving the Scene field stale.

Not every control acts on every scene (palette/color are no-ops on hires/blank;
sx/ix only move a scene's declared ``LIVE_PARAMS``). We can't disable controls in
the third-party WLED app — it renders a fixed set — but our own ``/`` page can:
each segment carries a ``c64`` vendor key (``_seg_caps``) of per-control booleans
and the page grays out the dead palette/color/slider controls. Scene, power and
brightness always apply, so they're never gated. The hints ride the ``/json``
poll + the WS push, so they refresh on auto-advance for free; WLED clients ignore
the unknown seg key.

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

import asyncio
import hashlib
import logging
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from .modes import PALETTE_MODES
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

# How often a connected WS client is checked for out-of-band state changes
# (playlist auto-advance, a queued jump landing) and pushed a fresh state if it
# moved. Real WLED pushes proactively; without this the app's Scene field goes
# stale between the user's own actions. Also bounds receive_json's blocking wait.
_WS_PUSH_INTERVAL_S = 1.5

# The WLED palette list is the c64cast palette modes (modes.PALETTE_MODES),
# title-cased for the app's dropdown. Index order matches PALETTE_MODES so a
# WLED `pal` index resolves straight back to a mode name.
_WLED_PALETTES = [m.title() for m in PALETTE_MODES]

# WLED info payload cosmetics — enough for real clients (app / python-wled /
# Home Assistant) to parse us as a WLED device. We pin a plausible firmware
# version and identify the product as c64cast.
_WLED_VERSION = "16.0.1"
# `vid` is a WLED build-date "version id". Two distinct client uses: feature/
# minimum-version gates compare it against a floor, and — the one that bites us —
# the WLED app/UI caches the effect + palette lists keyed on (vid, palcount) and
# only re-fetches when one changes (verified in WLED's index.js: the `wledPalx`
# cache check is `d.vid == lastinfo.vid && d.pcount == lastinfo.palcount`). Our
# effect list is the scene playlist, which changes between configs, so a *fixed*
# vid leaves the app showing a stale scene dropdown. We therefore report
# `_WLED_VID_BASE + hash(effect+palette names)` (see `WledBridge._content_vid`):
# a new scene/palette set yields a new vid → the app drops its cache and
# re-fetches, no manual clear. Kept >= the base and 7-digit/date-shaped so the
# minimum-version gates still pass, and this does NOT touch `ver` (the string the
# app's upgrade nag compares), so no spurious "please upgrade".
_WLED_VID_BASE = 2606010
_WLED_VID_SPREAD = 100000  # content-hash offset range added on top of the base

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
  /* A control the current scene can't act on: dimmed + non-interactive, with a
     tooltip explaining why (the input itself is also set .disabled). */
  .cap-off { opacity: 0.4; }
  .cap-off label { color: #666; }
</style>
</head>
<body>
<h1>c64cast <small style="color:#777">(WLED control surface)</small></h1>
<div class="row">
  <label for="on">Power</label>
  <input type="checkbox" id="on">
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

// Gray out a row + disable its input, with a tooltip, when the current scene
// can't use that control. Scene/Power/Brightness never pass disabled=true.
const CAP_OFF_TITLE = 'Not applicable to the current scene';
function markOff(row, input) {
  row.classList.add('cap-off');
  row.title = CAP_OFF_TITLE;
  if (input) input.disabled = true;
}

function selectRow(labelText, options, selectedIdx, onpick, disabled) {
  const row = document.createElement('div');
  row.className = 'row';
  const l = document.createElement('label');
  l.textContent = labelText;
  const sel = document.createElement('select');
  options.forEach((name, idx) => {
    const opt = document.createElement('option');
    opt.value = idx;
    opt.textContent = name;
    if (idx === selectedIdx) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.onchange = () => {
    onpick(parseInt(sel.value, 10));
    // Drop focus after a pick: refresh() skips while any control is focused (so
    // it won't yank a slider mid-drag), and a scene <select> stays focused after
    // selection — which would freeze the capability hints until a manual reload.
    sel.blur();
  };
  row.appendChild(l);
  row.appendChild(sel);
  if (disabled) markOff(row, sel);
  return row;
}

function rgbToHex(c) {
  const h = (n) => ('0' + (n & 255).toString(16)).slice(-2);
  return '#' + h(c[0]) + h(c[1]) + h(c[2]);
}

function segmentEl(i, seg, effects, palettes) {
  const wrap = document.createElement('div');
  wrap.className = 'segment';
  const title = document.createElement('h2');
  title.textContent = seg.n || ('System ' + (i + 1));
  wrap.appendChild(title);

  // Per-control applicability hints (vendor `c64` key). Absent (older payload)
  // => assume everything works, so we never over-disable.
  const caps = seg.c64 || {pal: true, col: true, sx: true, ix: true};

  // Scene is always live; Palette grays out when the mode can't swap it.
  wrap.appendChild(selectRow('Scene', effects, seg.fx,
    (v) => { post({seg: [{id: i, fx: v}]}); scheduleRefresh(); }));
  wrap.appendChild(selectRow('Palette', palettes, seg.pal,
    (v) => post({seg: [{id: i, pal: v}]}), !caps.pal));

  // Brightness is a real screen dim on every scene (never gated); Speed/Intensity
  // gray out when the current scene declares no matching live param.
  [['Brightness', 'bri', seg.bri, true], ['Speed', 'sx', seg.sx, caps.sx],
   ['Intensity', 'ix', seg.ix, caps.ix]].forEach(([label, key, val, enabled]) => {
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
    if (!enabled) markOff(row, slider);
    wrap.appendChild(row);
  });

  const colRow = document.createElement('div');
  colRow.className = 'row';
  const colLabel = document.createElement('label');
  colLabel.textContent = 'Color';
  const picker = document.createElement('input');
  picker.type = 'color';
  picker.value = rgbToHex((seg.col && seg.col[0]) || [0, 0, 0]);
  picker.oninput = () => {
    const h = picker.value;
    const rgb = [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16),
                 parseInt(h.slice(5, 7), 16)];
    post({seg: [{id: i, col: [rgb]}]});
  };
  colRow.appendChild(colLabel);
  colRow.appendChild(picker);
  if (!caps.col) markOff(colRow, picker);
  wrap.appendChild(colRow);
  return wrap;
}

async function refresh() {
  const active = document.activeElement;
  if (active && (active.tagName === 'INPUT' || active.tagName === 'SELECT')) return;
  const r = await fetch('/json');
  const d = await r.json();
  document.getElementById('on').checked = d.state.on;
  const segsEl = document.getElementById('segments');
  segsEl.innerHTML = '';
  d.state.seg.forEach((seg, i) => segsEl.appendChild(segmentEl(i, seg, d.effects, d.palettes)));
}

function scheduleRefresh() {
  // A scene jump isn't instant (the target scene tears down + sets up), so an
  // immediate refresh would still read the outgoing scene's capability hints.
  // Poll a few times to pick up the new scene once it's live, ahead of the
  // steady 4s tick; refresh() is a no-op rebuild if nothing changed yet.
  [600, 1500, 3000].forEach((ms) => setTimeout(refresh, ms));
}

document.getElementById('on').onchange = (e) => post({on: e.target.checked});

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""
# Source-first preserves the existing generator behavior; `scene.gain` reaches
# the scope scenes (WaveformScene/MidiScene/AsidScene), which *are* the
# renderer and so have no source/effect holder — see the `scene.` case in
# _set_live_param.
_IX_TARGETS = ("source.scale", "source.intensity", "effect.intensity", "scene.gain")


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


def _resolve_live_target(scene: Any, targets: tuple[str, ...]) -> tuple[Any, str] | None:
    """The first `(holder, name)` among `targets` whose holder declares that
    `name` in its `LIVE_PARAMS`, or None if none does (no scene, or no scene
    object exposes any of the targets).

    `scene.<name>` targets the scene itself (scope scenes mix in the renderer, so
    the param lives on the scene, not a source/effect holder); `source.<name>` /
    `effect.<name>` target that attribute. Shared by `_set_live_param` (which
    performs the write) and `_seg_caps` (which only needs to know whether a write
    *would* land, to gray out a dead slider on the `/` page)."""
    if scene is None:
        return None
    for target in targets:
        holder_attr, _, name = target.partition(".")
        holder = scene if holder_attr == "scene" else getattr(scene, holder_attr, None)
        if holder is None:
            continue
        live_params = getattr(type(holder), "LIVE_PARAMS", {})
        if name in live_params:
            return holder, name
    return None


def _set_live_param(pl: Playlist, targets: tuple[str, ...], value_0_255: int) -> None:
    """Drive the current scene's first-declared LIVE_PARAM among `targets` from a
    0..255 WLED slider value. Mirrors midi_control._apply_param's holder/LIVE_PARAMS
    lookup; a silent no-op when no scene / no matching param (documented)."""
    resolved = _resolve_live_target(pl.current, targets)
    if resolved is None:
        return
    holder, name = resolved
    norm = max(0.0, min(1.0, value_0_255 / _SLIDER_MAX))
    lo, hi = getattr(type(holder), "LIVE_PARAMS", {})[name]
    setattr(holder, name, lo + norm * (hi - lo))


def _seg_caps(pl: Playlist) -> dict[str, bool]:
    """Which of the WLED palette / color / speed / intensity controls actually
    *do* something on the current scene — ridden on each seg dict as a `c64`
    vendor key so our own `/` control page can gray out the dead ones.

    Mirrors the applicability guards the write paths already enforce: `pal` ⇐ the
    mode exposes `set_palette_mode` (as `_apply_palette` requires), `col` ⇐ it
    *also* exposes `set_color_map` (as `_apply_force_colors` requires), and
    `sx`/`ix` ⇐ `_resolve_live_target` finds a matching LIVE_PARAM. No scene ⇒
    everything False. WLED clients ignore the unknown seg key; only the `/` page
    reads it."""
    scene = pl.current
    if scene is None:
        return {"pal": False, "col": False, "sx": False, "ix": False}
    mode, api = _current_mode_api(pl)
    live = mode is not None and api is not None
    pal = live and hasattr(mode, "set_palette_mode")
    col = pal and hasattr(mode, "set_color_map")
    return {
        "pal": bool(pal),
        "col": bool(col),
        "sx": _resolve_live_target(scene, _SX_TARGETS) is not None,
        "ix": _resolve_live_target(scene, _IX_TARGETS) is not None,
    }


def _current_mode_api(pl: Playlist) -> tuple[Any, Any]:
    """(display_mode, api) for the playlist's current scene, or (None, None).

    Central guard for the palette/color live actions: both need a live scene
    with a display mode and its backend handle."""
    scene = pl.current
    if scene is None:
        return None, None
    return getattr(scene, "display_mode", None), getattr(scene, "api", None)


def _apply_palette(pl: Playlist, index: int) -> None:
    """Live-swap the current scene's palette_mode from a WLED `pal` index, and
    clear any active color-picker force (the "back to normal palette" path).

    No-op when there's no scene, the mode can't swap palette modes
    (hires/petscii/blank have no `set_palette_mode`), or the index is out of
    range. Mirrors the on-C64 menu's live palette swap (overlays/menu.py)."""
    if not (0 <= index < len(PALETTE_MODES)):
        return
    mode, api = _current_mode_api(pl)
    if mode is None or api is None or not hasattr(mode, "set_palette_mode"):
        return
    if hasattr(mode, "set_color_map"):
        mode.set_color_map(None)  # drop a prior color-picker force
    mode.set_palette_mode(api, PALETTE_MODES[index], force_palette=False)


def _apply_force_colors(pl: Playlist, cols: Any) -> None:
    """Remap the current scene to the WLED color-picker's colors (force_palette
    posterize). Snaps each of the up-to-3 `col` slots to its nearest C64 color,
    ensures ≥2 distinct colors, and installs a fixed ColorMap live.

    No-op (echo only, handled by the caller) when there's no scene or the mode
    can't apply a forced palette (only mcm/mhires do)."""
    if not isinstance(cols, (list, tuple)):
        return
    mode, api = _current_mode_api(pl)
    if (
        mode is None
        or api is None
        or not hasattr(mode, "set_color_map")
        or not hasattr(mode, "set_palette_mode")
    ):
        return
    from .palette import build_fixed_color_map, nearest_palette_index

    indices: list[int] = []
    for slot in cols[:3]:
        if isinstance(slot, (list, tuple)) and len(slot) >= 3:
            idx = nearest_palette_index(slot)
            if idx not in indices:
                indices.append(idx)
    if not indices:
        return
    if len(indices) < 2:
        # A single picked color needs a partner for any contrast: black, or
        # white if black itself was the pick.
        indices.append(1 if indices[0] == 0 else 0)
    cmap = build_fixed_color_map(indices)
    if cmap is None:
        return
    mode.set_color_map(cmap)
    # A forced palette pairs with "percell" — the invariant MCM's ctor and the
    # SHIFT cycle both hold. Forcing chromatic colors while, say, grayscale is
    # active would let grayscale's chromatic penalty + fixed gray backgrounds
    # fight the picked colors (they'd render flat gray), so snap to percell.
    mode.set_palette_mode(api, "percell", force_palette=True)


class WledBridge:
    """Translates between WLED JSON state and the c64cast playlists.

    Holds an ordered list of (name, Playlist) — one WLED segment per system. It
    keeps a little per-segment *echo* state (brightness / slider positions / the
    last palette+color choice) so the app's UI round-trips; the *functional*
    fields (on/off, fx, sx/ix, pal, col) drive the playlist directly — pal swaps
    the current scene's palette_mode, col forces its palette to the picked colors.
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
        # The WLED "palette" list is the c64cast palette modes (per-cell slot
        # allocation strategies) — index-stable so `seg[].pal` maps to a mode.
        # Selecting one live-swaps the current scene's mode (see _apply_palette).
        return _WLED_PALETTES

    def _content_vid(self) -> int:
        """The `vid` reported in `/json/info`, derived from the discoverable
        effect + palette names so it changes whenever the scene playlist (or
        palette set) does — forcing the WLED app to drop its cached lists and
        re-fetch (see the `_WLED_VID_BASE` note). Deterministic per content, so a
        given config always reports the same vid across runs (no spurious churn).
        Always `>= _WLED_VID_BASE` and date-int-shaped so minimum-version gates
        still pass."""
        payload = "\n".join(self._effects()) + "\x00" + "\n".join(self.palettes())
        digest = hashlib.sha1(payload.encode("utf-8")).digest()
        offset = int.from_bytes(digest[:4], "big") % _WLED_VID_SPREAD
        return _WLED_VID_BASE + offset

    def mac(self) -> str:
        return self._mac

    def device_uuid(self) -> str:
        return self._uuid

    def _seg_name(self, i: int) -> str:
        """Human label for segment *i*. In an ensemble each system has its own
        name; a single-system run's internal name is a generic default, so fall
        back to the (configurable) device `[wled].name` there — that's the
        answer to "System 1 isn't a useful name"."""
        if len(self._systems) == 1:
            return self._name
        return self._systems[i][0]

    def _seg_dict(self, i: int) -> dict[str, Any]:
        _name, pl = self._systems[i]
        echo = self._seg_echo[i]
        playing = not pl.pause_event.is_set()
        fx = pl.index if 0 <= pl.index < len(pl.scenes) else 0
        return {
            "id": i,
            "n": self._seg_name(i),
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
            # Vendor extension: per-control applicability hints for our own `/`
            # page (grays out palette/color/sliders that can't act on the current
            # scene). WLED clients ignore unknown seg keys — verified they still
            # parse the payload; the load-bearing check lives in the HW test.
            "c64": _seg_caps(pl),
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
            "vid": self._content_vid(),
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

    def _apply_dim(self, i: int) -> None:
        """Push system *i*'s effective brightness — global master × this
        segment's `bri` — onto its playlist + live display mode as a real C64
        output dim (`user_dim`), so the app's brightness wheel visibly darkens
        the screen rather than only echoing.

        Left untouched when either brightness is 0: that's the transport/pause
        path (see `_set_playing`), and keeping the prior `user_dim` means a
        later power-on restores at the brightness it had before, not full."""
        _name, pl = self._systems[i]
        seg_bri = self._seg_echo[i]["bri"]
        if self._global_bri <= 0 or seg_bri <= 0:
            return
        dim = (self._global_bri / _SLIDER_MAX) * (seg_bri / _SLIDER_MAX)
        pl.user_dim = dim
        scene = pl.current
        mode = getattr(scene, "display_mode", None) if scene is not None else None
        if mode is not None:
            mode.user_dim = dim

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
        # A per-segment brightness change is a real output dim (bri==0 stays a
        # pause, handled above — _apply_dim leaves user_dim intact there).
        if "bri" in seg:
            self._apply_dim(i)
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
        # Palette dropdown → palette_mode swap (+ clears any color-picker force),
        # and color picker → forced palette. Both map onto the *same* C64 palette,
        # so they're mutually exclusive intents — but the WLED app re-POSTs the
        # full segment (pal AND col) on every change. Acting on an unchanged field
        # would let the echoed `col` clobber a palette pick (and vice versa), so
        # only apply the field that actually *changed* from what we last echoed.
        if "pal" in seg:
            new_pal = int(seg["pal"])
            changed = new_pal != echo["pal"]
            echo["pal"] = new_pal
            if changed:
                _apply_palette(pl, new_pal)
        if "col" in seg and isinstance(seg["col"], list):
            changed = seg["col"] != echo["col"]
            echo["col"] = seg["col"]
            if changed:
                _apply_force_colors(pl, seg["col"])

    def apply(self, partial: Mapping[str, Any]) -> None:
        """Apply a partial WLED state object (from POST /json or a WS message).

        Top-level `on`/`bri`/`transition` apply to every system (WLED's master
        controls); a `seg` list targets individual systems by position/id.
        Thread-safe: playlist transport is event-based and the echo state is
        lock-guarded."""
        with self._lock:
            master_on = True
            master_bri_changed = "bri" in partial
            if "transition" in partial:
                self._transition = int(partial["transition"])
            if master_bri_changed:
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
            # The master brightness scales every segment's effective dim, so a
            # top-level `bri` change re-dims all systems — including any whose
            # own `bri` wasn't in this POST (_apply_dim reads the echoed seg bri).
            if master_bri_changed:
                for i in range(len(self._systems)):
                    self._apply_dim(i)


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

    async def _broadcast_state() -> None:
        state_msg = {"state": bridge.state_dict()}
        for client in list(ws_clients):
            try:
                await client.send_json(state_msg)
            except Exception:
                ws_clients.discard(client)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        ws_clients.add(websocket)
        # WLED sends the full state+info on connect.
        await websocket.send_json({"state": bridge.state_dict(), "info": bridge.info_dict()})
        last_pushed = bridge.state_dict()
        try:
            while True:
                # Race an incoming command against a short timeout: on a message,
                # apply it and broadcast; on timeout, push state IF it changed on
                # its own (playlist auto-advance, a queued jump landing) — real
                # WLED pushes state proactively, and the app's Scene field goes
                # stale without it since we're not driven by an external client.
                try:
                    msg = await asyncio.wait_for(
                        websocket.receive_json(), timeout=_WS_PUSH_INTERVAL_S
                    )
                except TimeoutError:
                    cur = bridge.state_dict()
                    if cur != last_pushed:
                        last_pushed = cur
                        await _broadcast_state()
                    continue
                # WLED wraps commands variously; accept a bare state object or
                # {"state": {...}} and ignore control frames we don't model.
                payload = msg.get("state", msg) if isinstance(msg, Mapping) else None
                if isinstance(payload, Mapping):
                    bridge.apply(payload)
                last_pushed = bridge.state_dict()
                await _broadcast_state()
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
