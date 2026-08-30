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

* **Clip launch** enqueues a :class:`~c64cast.control.performance.ClipEvent` onto
  ``pl.performance`` (drained on the playlist thread) — never a scene mutation on
  this HTTP thread, the rule the whole performance path follows.
* **Tap tempo** calls ``pl.tempo.tap()`` — an in-memory beat-grid write, no DMA.
* **Effect bypass** flips ``scene.effects[i].enabled`` — a GIL-atomic bool the
  render loop reads next frame.
* **Live tune** goes through :mod:`live_tune`, the one module that resolves a
  target string against the running scene, so a knob turned here is the same
  write a MIDI CC or a WLED slider makes — including the live-tune tracker entry
  that lets a ``mode.*`` change be saved back into the config. That record rides
  back out in the state frame (:func:`_tuned_dict`), because a daemon has no exit
  prompt to offer it at; the write itself is an HTTP route in :mod:`web_api`,
  with the config store's other writers. The one thing this surface asks for
  differently is **no** ``post_osd``: performance feedback stays off the audience
  screen, which is the whole point of a phone console.
* **Transport** (``pause`` / ``resume`` / ``skip``) and **jump** set the same
  playlist events the C64's own keys do, so the run loop applies them at its
  next clean boundary rather than this thread mutating a scene.
* **Looks** (Live DJ/VJ Phase 6) enqueue a :class:`~c64cast.control.performance.LookEvent`
  (``save`` / recall), drained on the playlist thread exactly like a clip launch —
  a look captures the active clip + effect-chain state and re-fires it on recall.

The controls are generated from the registries rather than listed here: the
effect rack from each live layer's own class ``LIVE_PARAMS``, and the tune panel
from :func:`introspect.live_targets` filtered to what the *current scene* can
actually be asked for. Neither can drift from the code, and neither can offer a
slider that writes nowhere.

Like :mod:`wled_device`, this module deliberately does **not** ``from __future__
import annotations``: the WebSocket route below annotates its param with a name
imported inside :func:`register_perf_routes`, and stringized annotations would
make FastAPI mis-read it as a query param and skip the WebSocket injection.
"""

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from pathlib import PurePath
from typing import Any

from c64cast.app.playlist import Playlist

from . import live_tune
from .performance import ClipEvent
from .transport import TransportEvent

log = logging.getLogger(__name__)

#: The transport verbs a console may send.
#:
#: ``pause``/``resume``/``skip`` set the same playlist events the C64
#: keyboard and a MIDI transport button set — a machine-level halt, applied at
#: the run loop's next clean boundary (see :meth:`PerfBridge.transport`).
#:
#: ``freeze``/``unfreeze``/``rw``/``ff``/``seek``/``loop_toggle``/``loop_slot``
#: (Live DJ/VJ Phase 7) instead enqueue a
#: :class:`~c64cast.control.transport.TransportEvent` onto the current
#: scene's own :class:`~c64cast.scenes.video_transport.VideoTransportControls`
#: — pause-in-place with audio muting, not a machine halt — which is what the
#: Live tab's scrub bar, hold-to-rewind/fast-forward and A/B loop drive.
TRANSPORT_VERBS = (
    "pause",
    "resume",
    "skip",
    "freeze",
    "unfreeze",
    "rw",
    "ff",
    "seek",
    "loop_toggle",
    "loop_slot",
)

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


#: Every declared live-tune target, read once. It describes the registries, not
#: the run, so it cannot change while the process is up — and building it pulls
#: in numpy/cv2 through the mode and generator modules, which is work the state
#: feed does three times a second.
_LIVE_TARGETS: list[Any] = []


def _live_target_docs() -> list[Any]:
    global _LIVE_TARGETS
    if not _LIVE_TARGETS:
        from c64cast.app import introspect

        _LIVE_TARGETS = introspect.live_targets()
    return _LIVE_TARGETS


def _live_dict(pl: Playlist) -> list[dict[str, Any]]:
    """The live-tune knobs the *current scene* actually has, with their values.

    Every declared target is tried against the running scene and only the ones
    that resolve are sent, so a console renders exactly what it can turn — a
    blank scene has no generator and a PETSCII scene has no dither, and neither
    should show a slider that writes nowhere. Grouping (`Color pipeline`,
    `Generator`, …) is ``introspect``'s, the same grouping the ``--midi-setup``
    picker offers, so the two surfaces name the same knob the same way.

    The per-layer effect knobs are *not* here — those are addressed by layer
    (``fx2.amount``) and have their own rack in :func:`_effects_dict`, where
    bypass lives too."""
    scene = pl.current
    out: list[dict[str, Any]] = []
    for doc in _live_target_docs():
        found = live_tune.resolve(scene, doc.target)
        if found is None:
            continue
        value = live_tune.current(found)
        row: dict[str, Any] = {
            "target": doc.target,
            "group": doc.group,
            "name": found.name,
            "kind": found.kind,
            "value": value,
            "vocabulary": doc.vocabulary,
        }
        if found.kind == "scalar":
            row["min"] = found.lo
            row["max"] = found.hi
            row["norm"] = live_tune.norm_of(found, value)
        else:
            row["choices"] = list(found.choices)
        out.append(row)
    return out


def _tuned_dict(pl: Playlist) -> dict[str, Any]:
    """What has been *turned* since the show started, and whether it can be kept.

    :func:`_live_dict` is the knobs; this is the record of moving them. Every
    ``mode.*`` change files into the playlist's
    :class:`~c64cast.control.transport.LiveTuneTracker` whichever surface made it
    — a MIDI CC, a WLED slider, the C64's own menu, or this console — and a CLI
    run's exit is where that record is offered back to the config. A daemon has
    no exit to prompt at (``serve.teardown`` passes ``save_live_tune=False``, and
    must: a host that rewrote every show file it stopped would be unusable), so
    the record is what the browser is shown instead, and saving it is a
    deliberate tap rather than a question asked at the worst possible moment.

    ``savable`` is the count a Save button acts on, and it is not always
    ``len(changes)``: a knob no config field carries has nowhere to be written,
    and so does ``mode.palette_mode`` turned on a scene the config never named (a
    launched clip, an interleaved video) — its home is one ``[[scenes]]`` block
    and that scene has none. Those rows are still listed, because a change that
    will be lost at the end of the show is exactly what a performer needs told."""
    rows = pl.live_tracker.pending()
    savable = [r for r in rows if r["field"] is not None]
    out: dict[str, Any] = {
        "changes": rows,
        "savable": len(savable),
        # Whether there is a file to write to at all. A quick-playback run has
        # no config, and gets the same pasteable [color] block the CLI prints.
        "config_path": pl.config_path or "",
        # The file's bare name, no directory and no `.toml` — the same spelling
        # a config gets everywhere else in the console. No `ConfigStore` reaches
        # this surface, so a root-relative label (`config/journey`) isn't
        # available; the name alone is what both consoles show for a tune save.
        "config_name": (PurePath(pl.config_path).stem if pl.config_path else ""),
    }
    if savable and not pl.config_path:
        out["snippet"] = pl.live_tracker.toml_snippet()
    return out


def scene_rows(pl: Playlist) -> list[dict[str, Any]]:
    """The playlist's scenes, for a console that offers a jump.

    Shared with the control plane's ``/scenes`` so the two answers cannot
    disagree about what is playing. ``duration_s`` is None for a scene that runs
    until its source ends — VideoScene uses ``math.inf`` for that and JSON
    cannot carry it."""
    return [
        {
            "index": i,
            "name": s.name,
            "duration_s": (None if math.isinf(s.duration_s) else s.duration_s),
            "is_current": i == pl.index,
        }
        for i, s in enumerate(pl.scenes)
    ]


def _clip_state(slot: int, active: int | None, armed: int | None) -> str:
    if slot == active:
        return "active"
    if slot == armed:
        return "armed"
    return "loaded"


def _transport_dict(pl: Playlist) -> dict[str, Any] | None:
    """The DJ transport surface of the *current* scene (Live DJ/VJ Phase 7) —
    ``None`` for a scene that declares none (a generator, a picture, a scope),
    which is what tells the console to render no transport bar rather than one
    that writes nowhere. Duck-typed against the same ``transport_*`` methods
    :class:`~c64cast.control.transport.TransportSession` dispatches onto, so a
    console can only ask for what the engine already exposes."""
    scene = pl.current
    position = getattr(scene, "transport_position", None)
    duration = getattr(scene, "transport_duration", None)
    is_paused = getattr(scene, "transport_is_paused", None)
    if position is None or duration is None or is_paused is None:
        return None
    loop_info = getattr(scene, "transport_loop_info", None)
    loop_slots = getattr(scene, "transport_loop_slots", None)
    return {
        "position": round(position(), 2),
        "duration": duration(),
        "frozen": is_paused(),
        "loop": loop_info() if loop_info is not None else {"state": "none", "a": None, "b": None},
        "loop_slots": loop_slots() if loop_slots is not None else [],
    }


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
        "scene_index": pl.index,
        "paused": pl.pause_event.is_set(),
        "scenes": scene_rows(pl),
        "tempo": _tempo_dict(pl),
        "active_slot": active,
        "armed": armed_block,
        "clips": clips,
        "effects": _effects_dict(pl),
        # The color-pipeline / generator / scope knobs the current scene has.
        # The same list --midi-setup maps a controller onto, so a phone and a
        # MIDI box reach the same surface (Live DJ/VJ Phase 7).
        "live": _live_dict(pl),
        # …and what has already been turned, so a console can offer to keep it.
        "tuned": _tuned_dict(pl),
        # Saved look slots (Live DJ/VJ Phase 6) — the console lights a recall pad
        # only for a slot that holds a look. Reads the store from disk; cheap at
        # the state-poll cadence.
        "looks": perf.saved_look_slots(),
        # The current scene's DJ transport (freeze/scrub/rw/ff/A-B loop), or
        # None when it has none — see _transport_dict.
        "transport": _transport_dict(pl),
    }


class PerfBridge:
    """Read/write bridge between the web console and the per-system playlists.

    Takes a **provider** of the ensemble as an ordered ``[(name, Playlist)]``
    list (one system for a single-system run), called per read and per command:
    the console's server can outlive the session it drives (a host that starts
    and stops shows), so the set of systems is a moving target and an empty one
    just means nothing is running. Reads build the console state snapshot;
    writes go through the same performance engine the MIDI surface uses (clip
    launch → ``pl.performance.enqueue``, tap → ``pl.tempo.tap``, fx → a
    GIL-atomic layer write). Every method is cheap in-memory work — no DMA, no
    lock needed beyond the engine's own queues.

    An idle console gets an empty ``systems`` list rather than the ``503`` the
    control-plane routes answer with: the page is the gig-day fallback surface
    and has to stay loadable and self-explanatory between shows."""

    def __init__(self, systems: Callable[[], list[tuple[str, Playlist]]]) -> None:
        self._systems = systems

    # -- reads ---------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        systems = self._systems()
        return {
            "multi": len(systems) > 1,
            "systems": [_system_state(name, pl) for name, pl in systems],
        }

    def _resolve(self, system: str | None) -> Playlist | None:
        """The target playlist for a command: the named system, or the first
        system when unnamed (the single-system common case). ``None`` when the
        name is unknown — or when no session is running at all."""
        systems = self._systems()
        if not systems:
            return None
        if system is None:
            return systems[0][1]
        return dict(systems).get(system)

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
        normalized ``0..1`` slider position. A silent no-op when the layer /
        param doesn't exist; no OSD, because this surface exists so a performer
        has a readout the audience does not."""
        return self.live(system, f"fx{int(layer)}.{param}", norm=norm)

    def live(
        self,
        system: str | None,
        target: str,
        *,
        norm: float | None = None,
        value: float | str | None = None,
    ) -> bool:
        """Turn any live-tune target on the current scene — the color pipeline,
        a generator, a scope, or one effect layer.

        ``norm`` is a slider position (0..1), ``value`` the real number or the
        choice by name; a picker sends the latter because a choice list has no
        meaningful position. Goes through :mod:`live_tune` exactly as the MIDI
        and WLED surfaces do, so a ``mode.*`` knob turned here records into the
        live-tune tracker exactly as a MIDI knob's would — and :func:`_tuned_dict`
        puts that record back on the wire, which is how a knob turned on a phone
        reaches the config the daemon has no exit prompt to offer it at. Returns
        False only for an unknown system: a target the current scene doesn't have
        is a no-op, not an error, because the scene can change between the frame
        that offered the control and the tap."""
        pl = self._resolve(system)
        if pl is None:
            return False
        move = (
            live_tune.Move(position=float(norm), full_scale=1.0, osd=False)
            if norm is not None
            else live_tune.Move(value=value, osd=False)
        )
        live_tune.apply(pl, target, move)
        return True

    def transport(
        self,
        system: str | None,
        verb: str,
        *,
        pressed: bool = True,
        target: float | None = None,
        slot: int = 0,
        save: bool | None = None,
        clear: bool | None = None,
    ) -> bool:
        """Every transport verb (``TRANSPORT_VERBS``) on the target system.

        ``pause``/``resume``/``skip`` set the same events the C64's own keys
        and a MIDI transport button set, so the run loop applies them at its
        next clean boundary rather than mutating a scene from this thread.
        ``resume`` on a show that is not paused is harmless — the event is
        simply never consumed — so unlike the control plane's ``/resume``
        there is no conflict to report; a console showing a Pause button that
        is really a Resume button is the UI's problem, and it has the
        ``paused`` flag to solve it with.

        Everything else (Live DJ/VJ Phase 7 — freeze/scrub/rw/ff/loop) instead
        enqueues a :class:`~c64cast.control.transport.TransportEvent`, the same
        queue the MIDI transport surface drains from — see
        :class:`~c64cast.control.transport.TransportSession`. ``freeze``/
        ``unfreeze`` enqueue the target state rather than a bare toggle, and
        :meth:`~c64cast.control.transport.TransportSession._dispatch` checks
        ``transport_is_paused`` itself, on the playlist thread, right before
        acting on it — so two consoles open on the same show, or a network
        retry, can't race each other's stale read of the pre-enqueue state
        into a double-toggle that cancels out."""
        pl = self._resolve(system)
        if pl is None or verb not in TRANSPORT_VERBS:
            return False
        if verb in ("pause", "resume", "skip"):
            event = {"pause": pl.pause_event, "resume": pl.resume_event, "skip": pl.skip_event}[
                verb
            ]
            event.set()
            return True
        if verb in ("freeze", "unfreeze"):
            pl.transport.enqueue(TransportEvent(action=verb))
            return True
        if verb in ("rw", "ff"):
            pl.transport.enqueue(TransportEvent(action=verb, pressed=pressed))
            return True
        if verb == "seek":
            if target is None:
                return False
            pl.transport.enqueue(TransportEvent(action="seek", target=target))
            return True
        if verb == "loop_toggle":
            pl.transport.enqueue(TransportEvent(action="loop_toggle"))
            return True
        # loop_slot
        pl.transport.enqueue(TransportEvent(action="loop_slot", slot=slot, save=save, clear=clear))
        return True

    def jump(self, system: str | None, index: int) -> bool:
        """Go to scene `index` now. A cut rather than an interstitial: a console
        jump is a correction ("that one, not this one"), and the transition
        would put a title card in front of the thing being corrected to."""
        pl = self._resolve(system)
        if pl is None:
            return False
        if 0 <= index < len(pl.scenes):
            pl.request_jump(index, skip_interstitial=True)
        return True

    def look(self, system: str | None, slot: int, save: bool) -> bool:
        """Save or recall a "look" (active clip + effect-chain state) on the
        target system — enqueues a :class:`~c64cast.control.performance.LookEvent`, drained
        on the playlist thread, exactly as ``midi_control``'s ``look_save`` /
        ``look_recall`` do. Returns False for an unknown system."""
        pl = self._resolve(system)
        if pl is None:
            return False
        pl.performance.enqueue_look(slot, save=save)
        return True

    def apply(self, cmd: Mapping[str, Any]) -> bool:
        """Dispatch one console command dict (shared by the POST endpoints and
        the WS command frame). ``{"action": "launch"|"tap"|"fx"|"live"|
        "transport"|"jump"|"look", ...}``."""
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
        if action == "live":
            # A slider sends `norm`, a picker sends `value` — the two are not
            # interchangeable and the key says which one this is.
            norm = cmd.get("norm")
            return self.live(
                system,
                str(cmd["target"]),
                norm=None if norm is None else float(norm),
                value=cmd.get("value"),
            )
        if action == "transport":
            target = cmd.get("target")
            save = cmd.get("save")
            clear = cmd.get("clear")
            return self.transport(
                system,
                str(cmd.get("verb", "")),
                pressed=bool(cmd.get("pressed", True)),
                target=None if target is None else float(target),
                slot=int(cmd.get("slot", 0)),
                save=None if save is None else bool(save),
                clear=None if clear is None else bool(clear),
            )
        if action == "jump":
            return self.jump(system, int(cmd["index"]))
        if action == "look":
            return self.look(system, int(cmd["slot"]), bool(cmd.get("save", False)))
        return False


# The console page. Self-contained (inline CSS/JS, no CDN), phone-first: a sticky
# tempo bar with a locally-animated beat pulse and transport, a touch clip grid,
# an auto-generated effect rack, the current scene's tune knobs, the record of
# turning them, the look pads and a scene jump — one control for every action
# `PerfBridge.apply` dispatches, which `tests/test_perf_console.py` reads back
# out of this string and compares against that method's own source.
#
# It also shows the machine's screen, which is not a bridge action at all: one
# <img> against /api/screen/stream, because `multipart/x-mixed-replace` needs no
# decoder and no second socket, and a page with no build step cannot afford
# either. Off until asked — the host holds the machine's video stream up only
# while somebody is watching.
#
# State arrives over /perf/ws; commands go out as POSTs to /perf/*. The two
# exceptions are both /api routes that only a --serve host registers: the
# live-tune save-back (a *config write*, which needs a status code) and the
# screen. Both are handled as absent rather than assumed — this page is served
# by the control plane, which a plain CLI run has without any of /api. Kept
# dependency-free so it renders in any phone browser.
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
  .prow select { flex: 1; font: inherit; color: var(--fg); background: #2a2a33;
                 border: 1px solid var(--line); border-radius: 8px; padding: 0.3em; }
  .empty { color: var(--dim); font-size: 0.9em; }
  /* 4:3 because that is the shape a television gives a C64 — the stream's own
     384x272 has no square pixels. `pixelated` so a phone scaling it up shows
     the cells rather than a smear of them. */
  #screen { width: 100%; aspect-ratio: 4 / 3; object-fit: fill; background: #000;
            border-radius: 6px; image-rendering: pixelated; }
  .scene { color: var(--dim); font-size: 0.8em; margin-top: 0.2em; }
  .row { display: flex; gap: 0.4em; flex-wrap: wrap; align-items: center; }
  .jump { font-size: 0.85em; padding: 0.35em 0.7em; }
  .jump.sel { border-color: var(--active); color: var(--active); }
  .group { font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.06em;
           color: var(--dim); margin: 0.8em 0 0.1em; }
  .tuned { border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
           padding: 0.6em 0.7em; }
  .trow { display: flex; align-items: baseline; gap: 0.5em; flex-wrap: wrap;
          font-size: 0.85em; padding: 0.15em 0; }
  .trow .was { color: var(--dim); font-variant-numeric: tabular-nums; }
  .tag { font-size: 0.65em; color: var(--dim); border: 1px solid var(--line);
         border-radius: 999px; padding: 0.05em 0.45em; }
  .tmsg { font-size: 0.8em; color: var(--dim); }
  .snippet { background: #000; border: 1px solid var(--line); border-radius: 8px;
             padding: 0.6em; overflow-x: auto; font-size: 0.78em; margin: 0.6em 0 0; }
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
    <span class="chip" id="role" hidden>read-only</span>
    <div class="beats" id="beats"></div>
    <button id="tap">TAP</button>
  </div>
  <div class="tabs">
    <button id="pause">PAUSE</button>
    <button id="skip">SKIP</button>
  </div>
  <div class="tabs" id="tabs"></div>
</header>
<main>
  <div class="scene" id="scene"></div>
  <h2>Screen <button id="screenwatch">WATCH</button></h2>
  <img id="screen" alt="The Commodore's screen, live" hidden>
  <p class="empty" id="screenmsg"></p>
  <h2>Clips <span class="countin" id="countin"></span></h2>
  <div class="grid" id="clips"></div>
  <h2>Effects</h2>
  <div id="fx"></div>
  <h2>Tune</h2>
  <div id="tune"></div>
  <h2>Tuned</h2>
  <div class="tuned" id="tuned"></div>
  <h2>Looks <button id="looksave">SAVE</button></h2>
  <div class="grid looks" id="looks"></div>
  <h2>Scenes</h2>
  <div class="row" id="scenes"></div>
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
  // A viewer token's writes are rejected by the server with a 403; say so
  // instead of letting every pad tap look like a dead grid.
  document.getElementById('role').hidden = s.role !== 'viewer';
  const sys = curSys();
  if (sys) {
    const t = sys.tempo;
    clock = {bpm: t.bpm, phase: t.beat_phase, running: t.running,
             bpb: t.beats_per_bar, at: performance.now()};
  } else {
    clock.running = false;   // stop the pulse rather than free-run a dead grid
  }
  render();
}

function render() {
  const sys = curSys();
  if (!sys) {
    // No session (a host between shows). Clear the grids: leaving the last
    // show's pads up invites a tap that goes nowhere.
    ['tabs', 'clips', 'fx', 'tune', 'tuned', 'looks', 'scenes'].forEach((id) => {
      document.getElementById(id).innerHTML = '';
    });
    document.getElementById('run').className = 'chip';
    document.getElementById('run').textContent = 'idle';
    document.getElementById('scene').textContent = 'No session running.';
    document.getElementById('countin').textContent = '';
    return;
  }
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
  document.getElementById('pause').textContent = sys.paused ? 'RESUME' : 'PAUSE';
  renderCountin(sys);
  renderClips(sys);
  renderFx(sys);
  renderTune(sys);
  renderTuned(sys);
  renderLooks(sys);
  renderScenes(sys);
}

function renderScenes(sys) {
  const box = document.getElementById('scenes');
  box.innerHTML = '';
  sys.scenes.forEach((s) => {
    const b = document.createElement('button');
    b.className = 'jump' + (s.is_current ? ' sel' : '');
    b.textContent = (s.index + 1) + '. ' + s.name;
    b.onclick = () => post({action: 'jump', index: s.index});
    box.appendChild(b);
  });
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

// The knobs of the *current* scene, grouped as introspect groups them. Built
// the same way the effect rack is, and held still under a finger for the same
// reason — the state feed echoes the value back at the push cadence, and
// rebuilding mid-gesture drags the handle out from under it.
function renderTune(sys) {
  const box = document.getElementById('tune');
  const active = document.activeElement;
  if (active && box.contains(active)) return;
  box.innerHTML = '';
  if (!sys.live.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'Current scene has no tunable parameters.';
    box.appendChild(e);
    return;
  }
  let group = null;
  sys.live.forEach((k) => {
    if (k.group !== group) {
      group = k.group;
      const h = document.createElement('div');
      h.className = 'group';
      h.textContent = group;
      box.appendChild(h);
    }
    box.appendChild(k.kind === 'choice' ? tuneChoice(k) : tuneScalar(k));
  });
}

function tuneRow(knob) {
  const row = document.createElement('div');
  row.className = 'prow';
  const l = document.createElement('label');
  l.textContent = knob.name;
  row.appendChild(l);
  return row;
}

function tuneScalar(knob) {
  const row = tuneRow(knob);
  const sl = document.createElement('input');
  sl.type = 'range'; sl.min = 0; sl.max = 1000; sl.step = 1;
  sl.value = Math.round(knob.norm * 1000);
  const val = document.createElement('span');
  val.className = 'val';
  val.textContent = Number(knob.value).toFixed(2);
  sl.oninput = () => {
    const norm = parseInt(sl.value, 10) / 1000;
    val.textContent = (knob.min + norm * (knob.max - knob.min)).toFixed(2);
    post({action: 'live', target: knob.target, norm: norm});
  };
  row.appendChild(sl); row.appendChild(val);
  return row;
}

function tuneChoice(knob) {
  const row = tuneRow(knob);
  const sel = document.createElement('select');
  knob.choices.forEach((c) => {
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    if (c === knob.value) o.selected = true;
    sel.appendChild(o);
  });
  // A choice list has no position to drag, so this sends `value`, not `norm`.
  sel.onchange = () => post({action: 'live', target: knob.target, value: sel.value});
  row.appendChild(sel);
  return row;
}

// What has been turned since the show started, and the offer to keep it. The
// CLI asks this at exit; a daemon has no exit, so it is a button here. The
// write is /api/session/live-tune, which only a --serve host registers — on a
// one-shot run the page still lists the changes (a change about to be lost is
// what a performer needs told) and the run's own exit prompt makes the offer.
let tuneMsg = '';

function renderTuned(sys) {
  const box = document.getElementById('tuned');
  const tuned = sys.tuned;
  box.innerHTML = '';
  if (!tuned.changes.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'Nothing tuned yet this show.';
    box.appendChild(e);
    return;
  }
  tuned.changes.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'trow';
    const name = document.createElement('span');
    name.textContent = c.target;
    const was = document.createElement('span');
    was.className = 'was';
    was.textContent = fmt(c.old) + ' → ' + fmt(c.new);
    row.appendChild(name); row.appendChild(was);
    if (c.scene !== null) row.appendChild(tag('scene ' + (c.scene + 1)));
    if (c.field === null) row.appendChild(tag('runtime only'));
    box.appendChild(row);
  });
  box.appendChild(tunedActions(sys, tuned));
  if (tuned.snippet) {
    const pre = document.createElement('pre');
    pre.className = 'snippet';
    pre.textContent = tuned.snippet;
    box.appendChild(pre);
  }
}

function tag(text) {
  const el = document.createElement('span');
  el.className = 'tag';
  el.textContent = text;
  return el;
}

function fmt(v) {
  return typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);
}

function tunedActions(sys, tuned) {
  const acts = document.createElement('div');
  acts.className = 'tacts row';
  if (tuned.config_path && tuned.savable) {
    const save = document.createElement('button');
    save.textContent = 'KEEP ' + tuned.savable;
    save.title = 'Write these into ' + tuned.config_name;
    save.onclick = () => liveTune(sys, 'save');
    acts.appendChild(save);
  }
  const drop = document.createElement('button');
  drop.textContent = 'DISCARD';
  drop.onclick = () => liveTune(sys, 'discard');
  acts.appendChild(drop);
  const msg = document.createElement('span');
  msg.className = 'tmsg';
  msg.textContent = tuneMsg;
  acts.appendChild(msg);
  return acts;
}

async function liveTune(sys, action) {
  tuneMsg = action === 'save' ? 'saving…' : 'discarding…';
  render();
  try {
    const r = await fetch('/api/session/live-tune', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action, system: sys.name}),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      tuneMsg = action === 'save' ? ('saved to ' + body.path) : ('discarded ' + body.discarded);
    } else if (r.status === 404) {
      // A control-plane-only run: no host to write the file. Nothing is lost —
      // the run offers the same changes back on its own way out.
      tuneMsg = 'this run saves at exit, not from here';
    } else {
      tuneMsg = body.detail || ('refused (' + r.status + ')');
    }
  } catch (e) {
    tuneMsg = 'could not reach the host';
  }
  render();
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
// One button for both, off the `paused` flag: resume on a running show is a
// no-op the run loop never consumes, so the worst a stale label costs is a
// tap. The C64's own keys set these same events.
document.getElementById('pause').onclick = () => {
  const sys = curSys();
  post({action: 'transport', verb: sys && sys.paused ? 'resume' : 'pause'});
};
document.getElementById('skip').onclick = () => post({action: 'transport', verb: 'skip'});
document.getElementById('looksave').onclick = (ev) => {
  saveMode = !saveMode;
  ev.currentTarget.classList.toggle('arm', saveMode);
};

// The screen. One <img> against `multipart/x-mixed-replace` is the whole
// client — no decoder, no second socket — which is what makes it sayable on a
// page with no build step. Off until asked: the host only holds the machine's
// video stream up while somebody is watching, so opening this is what starts
// it, and closing it is what stops it.
let screenOn = false;
let screenEpoch = 0;

function setScreen(on) {
  const img = document.getElementById('screen');
  const msg = document.getElementById('screenmsg');
  const button = document.getElementById('screenwatch');
  screenOn = on;
  button.textContent = on ? 'STOP' : 'WATCH';
  img.hidden = !on;
  if (!on) {
    // Clearing the src is what closes the connection; leaving it set keeps
    // the machine streaming to a hidden image.
    img.removeAttribute('src');
    msg.textContent = '';
    return;
  }
  const sys = curSys();
  // A cache-buster per start: to a browser's cache this is an ordinary
  // response, and reusing the URL can re-serve the last frame of the old
  // stream instead of opening a new one.
  screenEpoch += 1;
  msg.textContent = '';
  img.src = '/api/screen/stream?system=' + encodeURIComponent(sys ? sys.name : '')
          + '&t=' + screenEpoch;
}

document.getElementById('screen').onerror = () => {
  if (!screenOn) return;
  setScreen(false);
  // Two ways to get here and the page cannot tell them apart from an <img>
  // error, so it names both: this run has no /api at all (the screen route
  // lives on a --serve host, and this page is served by the control plane),
  // or it does and this machine has no VIC of its own to stream.
  document.getElementById('screenmsg').textContent =
    'No picture — either this run serves no screen, or this machine has no video '
    + 'stream of its own (an Ultimate 64 taps its VIC; nothing else here can).';
};
document.getElementById('screenwatch').onclick = () => setScreen(!screenOn);
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

    def _with_role(state: dict[str, Any], scope: Mapping[str, Any]) -> dict[str, Any]:
        """Tag a snapshot with the caller's auth role (``None`` when the server
        runs without a token). The page greys itself out for a ``viewer``
        rather than letting taps fail silently against the 403 the auth
        middleware answers writes with."""
        state["role"] = scope.get("c64cast_role")
        return state

    @app.get("/perf")
    def perf_page() -> Response:
        return Response(content=_PERF_HTML, media_type="text/html")

    @app.get("/perf/state")
    def perf_state(request: Request) -> dict[str, Any]:
        return _with_role(bridge.state(), request.scope)

    @app.post("/perf/command")
    async def perf_command(request: Request) -> dict[str, Any]:
        body = await request.json()
        ok = bridge.apply(body) if isinstance(body, Mapping) else False
        return {"ok": bool(ok)}

    @app.websocket("/perf/ws")
    async def perf_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        ws_clients.add(websocket)
        # The one gap the auth middleware can't cover: a socket is a single
        # `GET` handshake, so inbound command frames have to be dropped here.
        read_only = websocket.scope.get("c64cast_role") == "viewer"
        try:
            # Push a fresh snapshot on a fixed cadence; the client extrapolates the
            # beat pulse locally in between. A receive with timeout lets a client
            # command frame (if any) through without blocking the push loop.
            while True:
                # Split from the socket's own failures below: a state frame that
                # raises is *our* bug, and swallowing it silently leaves every
                # connected console waiting forever for a push that will never
                # come — a hang where an error belongs.
                try:
                    frame = _with_role(bridge.state(), websocket.scope)
                except Exception:
                    log.exception("performance console: could not build a state frame")
                    break
                await websocket.send_json(frame)
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=_PUSH_INTERVAL_S)
                except TimeoutError:
                    continue
                if isinstance(msg, Mapping) and not read_only:
                    bridge.apply(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("perf console: websocket closed", exc_info=True)
        finally:
            ws_clients.discard(websocket)
