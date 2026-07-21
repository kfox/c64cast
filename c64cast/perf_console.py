"""Phone / web performance console (Live DJ/VJ Phase 5 — see
docs/architecture/control.md → "Live performance").

The no-OSD constraint (the C64 output is audience-facing) leaves the performer
with no on-screen readout of clip / effect / tempo state. Phase 4 fills that gap
with controller LEDs; this module is the other off-screen surface: a
phone-friendly touch page served by the **control plane** (same FastAPI app /
port as ``/status`` — `control_plane.build_app` registers these routes), with a
WebSocket live-state feed. It is the intended feedback surface for controllers
that can't light their pads (Arturia / SysEx-only grids — see the Phase-4 note).

Everything the console drives is the **same engine** the MIDI surface drives, so
a web launch and a pad launch are indistinguishable downstream:

* **Clip launch** enqueues a :class:`~c64cast.performance.ClipEvent` onto
  ``pl.performance`` (drained on the playlist thread) — never a scene mutation on
  this HTTP thread, the rule the whole performance path follows.
* **Tap tempo** calls ``pl.tempo.tap()`` — an in-memory beat-grid write, no DMA.
* **Effect bypass / param** flips ``scene.effects[i].enabled`` or sets a declared
  ``LIVE_PARAMS`` field — the identical GIL-atomic writes ``midi_control`` and the
  WLED bridge already make off the render thread. **No** ``post_osd``: performance
  feedback stays off the audience screen (the whole point of this surface).
* **Looks** (Live DJ/VJ Phase 6) enqueue a :class:`~c64cast.performance.LookEvent`
  (``save`` / recall), drained on the playlist thread exactly like a clip launch —
  a look captures the active clip + effect-chain state and re-fires it on recall.

The effect-rack controls are generated from each live layer's own class
``LIVE_PARAMS`` — the same class attribute :func:`introspect.live_targets` reads —
so the UI can't drift from the effect registry.

Like :mod:`wled_device`, this module deliberately does **not** ``from __future__
import annotations``: the WebSocket route below annotates its param with a name
imported inside :func:`register_perf_routes`, and stringized annotations would
make FastAPI mis-read it as a query param and skip the WebSocket injection.
"""

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from typing import Any

from .performance import ClipEvent
from .playlist import Playlist

log = logging.getLogger(__name__)

# How often the WebSocket pushes a fresh state snapshot to connected consoles.
# The beat grid advances continuously, so a client extrapolates the beat pulse
# locally between pushes (bpm + last beat_phase + wall-clock elapsed); this
# cadence only needs to be fast enough that clip/effect/tempo *changes* and the
# count-in readout feel live. ~3/sec is trivially cheap (one small JSON to a
# couple of phones) and nowhere near any I/O ceiling.
_PUSH_INTERVAL_S = 0.35


def _tempo_dict(pl: Playlist) -> dict[str, Any]:
    """Snapshot the playlist's beat grid (all GIL-atomic reads). ``beat_phase`` /
    ``bar_phase`` are sampled once against a single ``now`` so the client's local
    extrapolation starts from a consistent instant."""
    tempo = pl.tempo
    now = time.monotonic()
    return {
        "bpm": round(float(tempo.bpm), 2),
        "running": bool(tempo.running),
        "source": tempo.source,
        "beats_per_bar": int(tempo.beats_per_bar),
        "beat_phase": tempo.beat_phase_at(now),
        "bar_phase": tempo.bar_phase_at(now),
    }


def _beats_remaining(pl: Playlist, detail: tuple[int, str, float, float]) -> float | None:
    """Beats until an armed clip's quantize boundary, for the count-in readout.
    ``None`` when the clock isn't running (a stopped clock launches at once, so
    there is no count-in); ``0`` for ``quantize = "off"`` (immediate)."""
    quantize, arm_beat, arm_bar = detail[1], detail[2], detail[3]
    tempo = pl.tempo
    if not tempo.running:
        return None
    if quantize == "off":
        return 0.0
    now = time.monotonic()
    if quantize == "beat":
        target = math.floor(arm_beat) + 1
        return max(0.0, target - tempo.beat_phase_at(now))
    # bar
    target_bar = math.floor(arm_bar) + 1
    remaining_bars = target_bar - tempo.bar_phase_at(now)
    return max(0.0, remaining_bars * tempo.beats_per_bar)


def _effects_dict(pl: Playlist) -> list[dict[str, Any]]:
    """The current scene's effect chain as rack rows — one per layer, each with
    its bypass state, ``mod_source``, and every declared ``LIVE_PARAMS`` field
    (value + range + normalized position for the slider). Generated from the
    layer's own class ``LIVE_PARAMS`` (the registry source of truth), so the rack
    can't drift from the effects registry."""
    scene = pl.current
    effects = getattr(scene, "effects", None) or []
    out: list[dict[str, Any]] = []
    for idx, eff in enumerate(effects):
        params: list[dict[str, Any]] = []
        live_params: dict[str, tuple[float, float]] = getattr(type(eff), "LIVE_PARAMS", {}) or {}
        for name, (lo, hi) in live_params.items():
            value = float(getattr(eff, name, lo))
            span = hi - lo
            norm = (value - lo) / span if span else 0.0
            params.append(
                {
                    "name": name,
                    "value": round(value, 4),
                    "min": float(lo),
                    "max": float(hi),
                    "norm": max(0.0, min(1.0, norm)),
                }
            )
        out.append(
            {
                "index": idx,
                "name": getattr(eff, "name", type(eff).__name__),
                "enabled": bool(getattr(eff, "enabled", True)),
                "mod_source": getattr(eff, "mod_source", "audio"),
                "params": params,
            }
        )
    return out


def _clip_state(slot: int, active: int | None, armed: int | None) -> str:
    if slot == active:
        return "active"
    if slot == armed:
        return "armed"
    return "loaded"


def _system_state(name: str, pl: Playlist) -> dict[str, Any]:
    perf = pl.performance
    active = perf.active_slot
    armed = perf.armed_slot
    detail = perf.armed_detail
    armed_block: dict[str, Any] | None = None
    if detail is not None:
        remaining = _beats_remaining(pl, detail)
        armed_block = {
            "slot": detail[0],
            "quantize": detail[1],
            "beats_remaining": (round(remaining, 2) if remaining is not None else None),
        }
    clips = perf.clips_info()
    for clip in clips:
        clip["state"] = _clip_state(int(clip["slot"]), active, armed)
    cur = pl.current
    return {
        "name": name,
        "current_scene": cur.name if cur is not None else None,
        "tempo": _tempo_dict(pl),
        "active_slot": active,
        "armed": armed_block,
        "clips": clips,
        "effects": _effects_dict(pl),
        # Saved look slots (Live DJ/VJ Phase 6) — the console lights a recall pad
        # only for a slot that holds a look. Reads the store from disk; cheap at
        # the state-poll cadence.
        "looks": perf.saved_look_slots(),
    }


class PerfBridge:
    """Read/write bridge between the web console and the per-system playlists.

    Holds the ensemble as an ordered ``[(name, Playlist)]`` list (one system for
    a single-system run). Reads build the console state snapshot; writes go
    through the same performance engine the MIDI surface uses (clip launch →
    ``pl.performance.enqueue``, tap → ``pl.tempo.tap``, fx → a GIL-atomic layer
    write). Every method is cheap in-memory work — no DMA, no lock needed beyond
    the engine's own queues."""

    def __init__(self, systems: list[tuple[str, Playlist]]) -> None:
        if not systems:
            raise ValueError("PerfBridge needs at least one system")
        self._systems = systems
        self._by_name = dict(systems)

    # -- reads ---------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "multi": len(self._systems) > 1,
            "systems": [_system_state(name, pl) for name, pl in self._systems],
        }

    def _resolve(self, system: str | None) -> Playlist | None:
        """The target playlist for a command: the named system, or the first
        system when unnamed (the single-system common case)."""
        if system is None:
            return self._systems[0][1]
        return self._by_name.get(system)

    # -- writes --------------------------------------------------------------

    def launch(self, system: str | None, slot: int, pressed: bool = True) -> bool:
        """Fire (or release) a clip slot — enqueues a :class:`ClipEvent`, exactly
        as ``midi_control``'s ``clip_launch`` does. Returns False for an unknown
        system."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.performance.enqueue(ClipEvent(slot=slot, pressed=pressed))
        return True

    def tap(self, system: str | None) -> bool:
        """Register a tap-tempo hit on the target system's beat grid."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.tempo.tap(time.monotonic())
        return True

    def fx_bypass(self, system: str | None, layer: int, enabled: bool) -> bool:
        """Set effect-chain layer ``layer``'s bypass (``enabled``) on the current
        scene. A plain GIL-atomic bool write (the render loop reads it next
        frame); no OSD. Out-of-range layer / no chain → no-op, but a valid system
        still returns True (the command was addressed)."""
        pl = self._resolve(system)
        if pl is None:
            return False
        effects = getattr(pl.current, "effects", None) or []
        if 0 <= layer < len(effects):
            effects[layer].enabled = bool(enabled)
        return True

    def fx_param(self, system: str | None, layer: int, param: str, norm: float) -> bool:
        """Set a declared ``LIVE_PARAMS`` field of layer ``layer`` from a
        normalized ``0..1`` slider position (scaled into the param's range —
        mirrors ``midi_control._apply_param``'s ``0..127`` scaling). A silent
        no-op when the layer / param doesn't exist; no OSD."""
        pl = self._resolve(system)
        if pl is None:
            return False
        effects = getattr(pl.current, "effects", None) or []
        if not 0 <= layer < len(effects):
            return True
        eff = effects[layer]
        live_params: dict[str, tuple[float, float]] = getattr(type(eff), "LIVE_PARAMS", {}) or {}
        rng = live_params.get(param)
        if rng is None:
            return True
        lo, hi = rng
        clamped = max(0.0, min(1.0, float(norm)))
        setattr(eff, param, lo + clamped * (hi - lo))
        return True

    def look(self, system: str | None, slot: int, save: bool) -> bool:
        """Save or recall a "look" (active clip + effect-chain state) on the
        target system — enqueues a :class:`~c64cast.performance.LookEvent`, drained
        on the playlist thread, exactly as ``midi_control``'s ``look_save`` /
        ``look_recall`` do. Returns False for an unknown system."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.performance.enqueue_look(slot, save=save)
        return True

    def apply(self, cmd: Mapping[str, Any]) -> bool:
        """Dispatch one console command dict (shared by the POST endpoints and,
        potentially, a WS command frame). ``{"action":
        "launch"|"tap"|"fx"|"look", ...}``."""
        action = cmd.get("action")
        system = cmd.get("system")
        if action == "launch":
            return self.launch(system, int(cmd["slot"]), bool(cmd.get("pressed", True)))
        if action == "tap":
            return self.tap(system)
        if action == "fx":
            layer = int(cmd["layer"])
            if "param" in cmd:
                return self.fx_param(system, layer, str(cmd["param"]), float(cmd.get("value", 0.0)))
            return self.fx_bypass(system, layer, bool(cmd.get("enabled", True)))
        if action == "look":
            return self.look(system, int(cmd["slot"]), bool(cmd.get("save", False)))
        return False


# The console page. Self-contained (inline CSS/JS, no CDN), phone-first: a sticky
# tempo bar with a locally-animated beat pulse, a touch clip grid, and an
# auto-generated effect rack. State arrives over /perf/ws; commands go out as
# POSTs to /perf/*. Kept dependency-free so it renders in any phone browser.
_PERF_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>c64cast — performance</title>
<style>
  :root { --bg:#0d0d10; --panel:#17171d; --line:#2a2a33; --fg:#eee; --dim:#888;
          --loaded:#334; --armed:#d9a021; --active:#28c46a; --fxon:#3b82f6; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { background: var(--bg); color: var(--fg);
         font: 15px -apple-system, system-ui, sans-serif;
         margin: 0; padding: 0 0 2em; }
  header { position: sticky; top: 0; z-index: 5; background: var(--panel);
           border-bottom: 1px solid var(--line); padding: 0.6em 0.9em; }
  .tempo { display: flex; align-items: center; gap: 0.7em; }
  .bpm { font-size: 1.9em; font-weight: 700; font-variant-numeric: tabular-nums;
         min-width: 2.6em; }
  .bpm small { font-size: 0.45em; font-weight: 400; color: var(--dim); }
  .chip { font-size: 0.75em; color: var(--dim); border: 1px solid var(--line);
          border-radius: 999px; padding: 0.15em 0.6em; }
  .chip.run { color: var(--active); border-color: var(--active); }
  .beats { display: flex; gap: 0.35em; margin-left: auto; }
  .beat { width: 12px; height: 12px; border-radius: 50%; background: var(--line);
          transition: background 60ms, transform 60ms; }
  .beat.on { background: var(--fg); }
  .beat.down.on { background: var(--active); }
  button { font: inherit; color: var(--fg); background: #2a2a33;
           border: 1px solid var(--line); border-radius: 8px; padding: 0.5em 0.9em;
           cursor: pointer; }
  button:active { filter: brightness(1.3); }
  #tap { margin-left: 0.6em; font-weight: 600; }
  main { padding: 0.8em 0.9em; max-width: 760px; margin: 0 auto; }
  h2 { font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.08em;
       color: var(--dim); margin: 1.4em 0 0.5em; }
  .tabs { display: flex; gap: 0.4em; margin-top: 0.6em; flex-wrap: wrap; }
  .tabs button.sel { border-color: var(--fg); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
          gap: 0.55em; }
  .pad { aspect-ratio: 1 / 1; border-radius: 10px; border: 1px solid var(--line);
         background: var(--loaded); display: flex; flex-direction: column;
         align-items: center; justify-content: center; text-align: center;
         padding: 0.3em; font-size: 0.82em; line-height: 1.15; user-select: none;
         touch-action: none; overflow: hidden; }
  .pad .meta { font-size: 0.7em; color: var(--dim); margin-top: 0.25em; }
  .pad.armed { background: var(--armed); color: #111; animation: blink 0.5s steps(1) infinite; }
  .pad.active { background: var(--active); color: #062; border-color: var(--active); }
  @keyframes blink { 50% { opacity: 0.35; } }
  .countin { color: var(--armed); font-weight: 600; }
  .fx { border: 1px solid var(--line); border-radius: 10px; padding: 0.6em 0.7em;
        margin-bottom: 0.55em; background: var(--panel); }
  .fx .head { display: flex; align-items: center; gap: 0.6em; }
  .fx .name { font-weight: 600; }
  .fx .src { font-size: 0.72em; color: var(--dim); border: 1px solid var(--line);
             border-radius: 999px; padding: 0.05em 0.5em; }
  .fx .byp { margin-left: auto; min-width: 5.4em; }
  .fx.on .byp { background: var(--fxon); border-color: var(--fxon); }
  .fx.off { opacity: 0.55; }
  .prow { display: flex; align-items: center; gap: 0.6em; margin-top: 0.5em; }
  .prow label { width: 5.5em; font-size: 0.82em; color: var(--dim); flex-shrink: 0; }
  .prow input[type=range] { flex: 1; }
  .prow .val { width: 3.4em; text-align: right; font-variant-numeric: tabular-nums;
               font-size: 0.82em; }
  .empty { color: var(--dim); font-size: 0.9em; }
  .scene { color: var(--dim); font-size: 0.8em; margin-top: 0.2em; }
  .looks { grid-template-columns: repeat(auto-fill, minmax(58px, 1fr)); }
  .look { aspect-ratio: 1 / 1; border-radius: 10px; border: 1px solid var(--line);
          background: var(--loaded); display: flex; align-items: center;
          justify-content: center; font-weight: 600; user-select: none;
          touch-action: manipulation; opacity: 0.5; }
  .look.saved { opacity: 1; border-color: var(--fxon); }
  #looksave.arm { background: var(--armed); color: #111; border-color: var(--armed); }
</style>
</head>
<body>
<header>
  <div class="tempo">
    <div class="bpm" id="bpm">--<small> bpm</small></div>
    <span class="chip" id="src">internal</span>
    <span class="chip" id="run">idle</span>
    <div class="beats" id="beats"></div>
    <button id="tap">TAP</button>
  </div>
  <div class="tabs" id="tabs"></div>
</header>
<main>
  <div class="scene" id="scene"></div>
  <h2>Clips <span class="countin" id="countin"></span></h2>
  <div class="grid" id="clips"></div>
  <h2>Effects</h2>
  <div id="fx"></div>
  <h2>Looks <button id="looksave">SAVE</button></h2>
  <div class="grid looks" id="looks"></div>
</main>
<script>
let state = null;      // last full state from the server
let sel = 0;           // selected system index
let ws = null;
let pollTimer = null;
// Local beat-clock anchor for smooth pulse animation between server pushes.
let clock = {bpm: 120, phase: 0, running: false, bpb: 4, at: 0};

function post(cmd) {
  const sys = curSys();
  if (sys) cmd.system = sys.name;
  return fetch('/perf/command', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(cmd),
  }).catch(() => {});
}

function curSys() {
  if (!state || !state.systems.length) return null;
  return state.systems[Math.min(sel, state.systems.length - 1)];
}

function apply(s) {
  state = s;
  const sys = curSys();
  if (sys) {
    const t = sys.tempo;
    clock = {bpm: t.bpm, phase: t.beat_phase, running: t.running,
             bpb: t.beats_per_bar, at: performance.now()};
  }
  render();
}

function render() {
  const sys = curSys();
  if (!sys) return;
  // Tabs (only when more than one system).
  const tabs = document.getElementById('tabs');
  if (state.multi) {
    tabs.innerHTML = '';
    state.systems.forEach((s, i) => {
      const b = document.createElement('button');
      b.textContent = s.name;
      if (i === sel) b.className = 'sel';
      b.onclick = () => { sel = i; render(); };
      tabs.appendChild(b);
    });
  } else {
    tabs.innerHTML = '';
  }
  document.getElementById('src').textContent = sys.tempo.source;
  const run = document.getElementById('run');
  run.textContent = sys.tempo.running ? 'running' : 'idle';
  run.className = 'chip' + (sys.tempo.running ? ' run' : '');
  document.getElementById('scene').textContent =
    sys.current_scene ? ('▶ ' + sys.current_scene) : '';
  renderCountin(sys);
  renderClips(sys);
  renderFx(sys);
  renderLooks(sys);
}

// Number of look slots the console exposes (1-based pads).
const LOOK_SLOTS = 8;
let saveMode = false;   // when armed, a look-pad tap saves instead of recalls

function renderLooks(sys) {
  const grid = document.getElementById('looks');
  const saved = new Set(sys.looks || []);
  grid.innerHTML = '';
  for (let slot = 1; slot <= LOOK_SLOTS; slot++) {
    const pad = document.createElement('div');
    pad.className = 'look' + (saved.has(slot) ? ' saved' : '');
    pad.textContent = slot;
    pad.onclick = () => post({action: 'look', slot: slot, save: saveMode});
    grid.appendChild(pad);
  }
}

function renderCountin(sys) {
  const el = document.getElementById('countin');
  if (sys.armed && sys.armed.beats_remaining != null) {
    const n = Math.max(0, Math.ceil(sys.armed.beats_remaining));
    el.textContent = '· arming slot ' + sys.armed.slot + ' in ' + n;
  } else if (sys.armed) {
    el.textContent = '· arming slot ' + sys.armed.slot;
  } else {
    el.textContent = '';
  }
}

function renderClips(sys) {
  const grid = document.getElementById('clips');
  grid.innerHTML = '';
  if (!sys.clips.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'No clip grid configured ([[performance.clips]]).';
    grid.appendChild(e);
    return;
  }
  sys.clips.forEach((c) => {
    const pad = document.createElement('div');
    pad.className = 'pad ' + c.state;
    const nm = document.createElement('div');
    nm.textContent = c.name;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = c.launch + (c.loop ? ' ⟳' : '') + ' · ' + c.quantize;
    pad.appendChild(nm);
    pad.appendChild(meta);
    // pointerdown = press (arm/launch), pointerup/leave = release (gate/toggle).
    // trigger ignores the release, so press+release is safe for every type.
    const down = (ev) => { ev.preventDefault(); post({action: 'launch', slot: c.slot, pressed: true}); };
    const up = (ev) => { ev.preventDefault(); post({action: 'launch', slot: c.slot, pressed: false}); };
    pad.addEventListener('pointerdown', down);
    pad.addEventListener('pointerup', up);
    pad.addEventListener('pointercancel', up);
    grid.appendChild(pad);
  });
}

function renderFx(sys) {
  const box = document.getElementById('fx');
  // Don't rebuild while a slider is being dragged (would drop the gesture).
  const active = document.activeElement;
  if (active && active.tagName === 'INPUT' && box.contains(active)) return;
  box.innerHTML = '';
  if (!sys.effects.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'Current scene has no effect chain.';
    box.appendChild(e);
    return;
  }
  sys.effects.forEach((fx) => {
    const card = document.createElement('div');
    card.className = 'fx ' + (fx.enabled ? 'on' : 'off');
    const head = document.createElement('div');
    head.className = 'head';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = (fx.index + 1) + '. ' + fx.name;
    const src = document.createElement('span');
    src.className = 'src';
    src.textContent = fx.mod_source;
    const byp = document.createElement('button');
    byp.className = 'byp';
    byp.textContent = fx.enabled ? 'ON' : 'BYPASS';
    byp.onclick = () => post({action: 'fx', layer: fx.index, enabled: !fx.enabled});
    head.appendChild(name);
    head.appendChild(src);
    head.appendChild(byp);
    card.appendChild(head);
    fx.params.forEach((p) => {
      const row = document.createElement('div');
      row.className = 'prow';
      const l = document.createElement('label');
      l.textContent = p.name;
      const sl = document.createElement('input');
      sl.type = 'range'; sl.min = 0; sl.max = 1000; sl.step = 1;
      sl.value = Math.round(p.norm * 1000);
      const val = document.createElement('span');
      val.className = 'val';
      val.textContent = p.value.toFixed(2);
      sl.oninput = () => {
        const norm = parseInt(sl.value, 10) / 1000;
        val.textContent = (p.min + norm * (p.max - p.min)).toFixed(2);
        post({action: 'fx', layer: fx.index, param: p.name, value: norm});
      };
      row.appendChild(l); row.appendChild(sl); row.appendChild(val);
      card.appendChild(row);
    });
    box.appendChild(card);
  });
}

// Local beat-pulse animation: extrapolate the beat clock between server pushes
// so the dots move smoothly at the shown BPM without a round-trip per beat.
function animate() {
  const beats = document.getElementById('beats');
  const bpb = clock.bpb || 4;
  if (beats.childElementCount !== bpb) {
    beats.innerHTML = '';
    for (let i = 0; i < bpb; i++) {
      const d = document.createElement('div');
      d.className = 'beat' + (i === 0 ? ' down' : '');
      beats.appendChild(d);
    }
  }
  let phase = clock.phase;
  if (clock.running) phase += ((performance.now() - clock.at) / 1000) * (clock.bpm / 60);
  const beatInBar = ((Math.floor(phase) % bpb) + bpb) % bpb;
  const frac = phase - Math.floor(phase);
  document.getElementById('bpm').innerHTML =
    (clock.bpm ? clock.bpm.toFixed(0) : '--') + '<small> bpm</small>';
  [...beats.children].forEach((d, i) => {
    // Light the current beat on the front half of the beat (a pulse), always
    // when the clock is stopped just show the anchor beat dimly.
    const on = clock.running && i === beatInBar && frac < 0.5;
    d.classList.toggle('on', on);
  });
  requestAnimationFrame(animate);
}

function startWS() {
  try {
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(scheme + location.host + '/perf/ws');
  } catch (e) { scheduleFallback(); return; }
  ws.onopen = () => stopFallback();
  ws.onmessage = (ev) => { try { apply(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => { scheduleFallback(); setTimeout(startWS, 2500); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}
async function poll() {
  try { const r = await fetch('/perf/state'); apply(await r.json()); } catch (e) {}
}
function scheduleFallback() { if (!pollTimer) pollTimer = setInterval(poll, 1000); }
function stopFallback() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

document.getElementById('tap').onclick = () => post({action: 'tap'});
document.getElementById('looksave').onclick = (ev) => {
  saveMode = !saveMode;
  ev.currentTarget.classList.toggle('arm', saveMode);
};
poll();          // initial paint before WS connects
startWS();
requestAnimationFrame(animate);
</script>
</body>
</html>
"""


def register_perf_routes(app: Any, bridge: PerfBridge) -> None:
    """Register the performance-console routes on an existing FastAPI ``app``
    (the control plane's). Called from :func:`control_plane.build_app`. Imports
    FastAPI symbols locally (the app already required them) — real, non-stringized
    annotations so the WebSocket param injects correctly (see the module note)."""
    from fastapi import Request, Response, WebSocket, WebSocketDisconnect

    ws_clients: set[Any] = set()

    @app.get("/perf")
    def perf_page() -> Response:
        return Response(content=_PERF_HTML, media_type="text/html")

    @app.get("/perf/state")
    def perf_state() -> dict[str, Any]:
        return bridge.state()

    @app.post("/perf/command")
    async def perf_command(request: Request) -> dict[str, Any]:
        body = await request.json()
        ok = bridge.apply(body) if isinstance(body, Mapping) else False
        return {"ok": bool(ok)}

    @app.websocket("/perf/ws")
    async def perf_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        ws_clients.add(websocket)
        try:
            # Push a fresh snapshot on a fixed cadence; the client extrapolates the
            # beat pulse locally in between. A receive with timeout lets a client
            # command frame (if any) through without blocking the push loop.
            while True:
                await websocket.send_json(bridge.state())
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=_PUSH_INTERVAL_S)
                except TimeoutError:
                    continue
                if isinstance(msg, Mapping):
                    bridge.apply(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("perf console: websocket closed", exc_info=True)
        finally:
            ws_clients.discard(websocket)
