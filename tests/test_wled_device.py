"""Tests for the virtual WLED device / control surface (bridge Mode 1).

Covers the combined `[wled]` endpoint parser, the WledBridge state/info/effects
mapping, the apply() → playlist translation (transport, scene jump, live-param
sliders), and the WLED JSON HTTP + WS API via FastAPI's TestClient. No mDNS /
socket binding is exercised — those are the untestable-without-a-LAN parts.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast

from c64cast import config as cfgmod
from c64cast.playlist import Playlist
from c64cast.wled_device import PresetStore, WledBridge, build_wled_app

try:
    # The WLED JSON API tests drive the real FastAPI app via TestClient, which
    # also needs httpx (fastapi declares it optional). CI runs without the
    # `wled`/`control` extra, so guard the API class like test_control_plane
    # does; the bridge + parser tests below need none of this.
    from fastapi.testclient import TestClient  # noqa: F401

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False

# --- fakes ------------------------------------------------------------------


class _FakeSource:
    # Declares a LIVE_PARAM the WLED speed slider (source.speed) should drive.
    LIVE_PARAMS = {"speed": (0.0, 10.0)}

    def __init__(self) -> None:
        self.speed = 1.0


class _FakeScopeScene:
    """A scope-style scene (WaveformScene/MidiScene/AsidScene): it *is* the
    renderer, so `gain` lives on the scene and is reached via the `scene.`
    live-param prefix — there is no source/effect holder."""

    LIVE_PARAMS = {"gain": (0.25, 3.0)}

    def __init__(self) -> None:
        self.name = "Waveform"
        self.gain = 1.0
        self.source = None
        self.display_mode = None
        self.api = None


class _FakeMode:
    """A display mode that supports the live palette/force-palette seams (like
    MCM/MultiHires). Records the calls so tests can assert on them."""

    def __init__(self, name: str = "mhires") -> None:
        self.name = name
        self.palette_mode = "percell"
        self.color_map = "UNSET"  # sentinel: distinguishes "never set" from None
        self.palette_calls: list[tuple[str, bool | None]] = []
        self.user_dim = 1.0  # mirrors DisplayMode.user_dim (the WLED bri dim)

    def set_color_map(self, cmap: object) -> None:
        self.color_map = cmap

    def set_palette_mode(self, api: object, mode: str, *, force_palette: bool | None = None) -> str:
        self.palette_mode = mode
        self.palette_calls.append((mode, force_palette))
        return mode


class _FakeScene:
    def __init__(
        self,
        name: str,
        source: _FakeSource | None = None,
        display_mode: object | None = None,
    ) -> None:
        self.name = name
        self.source = source
        self.display_mode = display_mode
        self.api = object() if display_mode is not None else None


class _FakePlaylist:
    """Minimal Playlist surface the bridge touches."""

    def __init__(
        self,
        name: str,
        scene_names: list[str],
        *,
        source: _FakeSource | None = None,
        display_mode: object | None = None,
    ):
        self.name = name
        self.scenes = [_FakeScene(n) for n in scene_names]
        if self.scenes:
            if source is not None:
                self.scenes[0].source = source
            if display_mode is not None:
                self.scenes[0].display_mode = display_mode
                self.scenes[0].api = object()
        self.index = 0
        self.current = self.scenes[0] if self.scenes else None
        self.single_scene = len(self.scenes) <= 1
        self.user_dim = 1.0  # mirrors Playlist.user_dim (persistent WLED bri dim)
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()
        self.jumps: list[int] = []
        self.osd_posts: list[str] = []

    def post_osd(self, text: str, duration_s: float = 2.5) -> None:
        # Mirrors Playlist.post_osd (live-tune feedback); a live-param slider
        # write routes its "name value" message here.
        self.osd_posts.append(text)

    def request_jump(self, index: int, *, skip_interstitial: bool = True) -> None:
        if not 0 <= index < len(self.scenes):
            raise ValueError("out of range")
        self.jumps.append(index)


def _bridge(
    *, source: _FakeSource | None = None, display_mode: object | None = None
) -> tuple[WledBridge, _FakePlaylist]:
    pl = _FakePlaylist(
        "main", ["Waveform", "Plasma", "Tunnel"], source=source, display_mode=display_mode
    )
    systems = cast("list[tuple[str, Playlist]]", [("main", pl)])
    return WledBridge(systems, "c64cast"), pl


# --- endpoint parser --------------------------------------------------------


class EndpointParserTests(unittest.TestCase):
    def _parse(self, value: str | None) -> tuple[bool, str, int]:
        return cfgmod.parse_wled_endpoint(value, "0.0.0.0", 8080, field_name="[wled].listen")

    def test_none_and_disabled_are_off(self):
        self.assertEqual(self._parse(None), (False, "0.0.0.0", 8080))
        self.assertEqual(self._parse("disabled"), (False, "0.0.0.0", 8080))
        self.assertEqual(self._parse(""), (False, "0.0.0.0", 8080))
        self.assertEqual(self._parse("DISABLED"), (False, "0.0.0.0", 8080))

    def test_enabled_uses_defaults(self):
        self.assertEqual(self._parse("enabled"), (True, "0.0.0.0", 8080))
        self.assertEqual(self._parse("  Enabled  "), (True, "0.0.0.0", 8080))

    def test_host_only(self):
        self.assertEqual(self._parse("192.168.1.9"), (True, "192.168.1.9", 8080))

    def test_host_and_port(self):
        self.assertEqual(self._parse("192.168.1.9:9090"), (True, "192.168.1.9", 9090))

    def test_port_only(self):
        self.assertEqual(self._parse(":7000"), (True, "0.0.0.0", 7000))

    def test_host_with_trailing_colon(self):
        self.assertEqual(self._parse("10.0.0.5:"), (True, "10.0.0.5", 8080))

    def test_bad_port_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._parse("host:notaport")

    def test_out_of_range_port_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._parse(":70000")

    def test_resolvers_use_mode_defaults(self):
        cfg = cfgmod.Config()
        self.assertEqual(cfgmod.resolve_wled_broadcast(cfg), (False, "239.0.0.1", 11988))
        self.assertEqual(cfgmod.resolve_wled_listen(cfg), (False, "0.0.0.0", 8080))
        cfg.wled.broadcast = "enabled"
        cfg.wled.listen = "enabled"
        self.assertEqual(cfgmod.resolve_wled_broadcast(cfg), (True, "239.0.0.1", 11988))
        self.assertEqual(cfgmod.resolve_wled_listen(cfg), (True, "0.0.0.0", 8080))


# --- bridge reads -----------------------------------------------------------


class BridgeReadTests(unittest.TestCase):
    def test_effects_are_scene_names(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.effects(), ["Waveform", "Plasma", "Tunnel"])

    def test_effects_prefer_wled_label_when_present(self):
        # A scene with a stable randomization-aware `wled_label` (e.g. a random
        # SID pool) shows that in the effect list rather than its rotating name.
        bridge, pl = _bridge()
        pl.scenes[0].wled_label = "SID: random pool"  # type: ignore[attr-defined]
        self.assertEqual(bridge.effects(), ["SID: random pool", "Plasma", "Tunnel"])

    def test_state_shape_one_segment_per_system(self):
        bridge, _ = _bridge()
        state = bridge.state_dict()
        self.assertTrue(state["on"])  # not paused
        self.assertEqual(len(state["seg"]), 1)
        seg = state["seg"][0]
        self.assertEqual(seg["id"], 0)
        self.assertEqual(seg["fx"], 0)
        self.assertTrue(seg["on"])

    def test_state_reports_paused_as_off(self):
        bridge, pl = _bridge()
        pl.pause_event.set()
        state = bridge.state_dict()
        self.assertFalse(state["on"])
        self.assertFalse(state["seg"][0]["on"])

    def test_fx_tracks_current_index(self):
        bridge, pl = _bridge()
        pl.index = 2
        self.assertEqual(bridge.state_dict()["seg"][0]["fx"], 2)

    def test_info_identifies_as_wled(self):
        bridge, _ = _bridge()
        info = bridge.info_dict()
        self.assertEqual(info["product"], "c64cast")
        self.assertEqual(info["name"], "c64cast")
        self.assertEqual(info["fxcount"], 3)
        self.assertIn("ver", info)

    def test_full_has_all_sections(self):
        bridge, _ = _bridge()
        full = bridge.full()
        self.assertEqual(set(full), {"state", "info", "effects", "palettes"})

    def test_vid_is_content_derived_stable_and_gate_safe(self):
        from c64cast.wled_device import _WLED_VID_BASE, _WLED_VID_SPREAD

        bridge, _ = _bridge()
        vid = bridge.info_dict()["vid"]
        # Within [base, base+spread): stays date-int-shaped so WLED clients'
        # minimum-version/feature gates (which compare vid to a floor) pass.
        self.assertGreaterEqual(vid, _WLED_VID_BASE)
        self.assertLess(vid, _WLED_VID_BASE + _WLED_VID_SPREAD)
        # Deterministic per content — a given config always reports the same vid.
        self.assertEqual(vid, bridge.info_dict()["vid"])

    def test_vid_changes_when_effect_list_changes(self):
        # The WLED app caches the effect/palette lists keyed on (vid, palcount);
        # a different scene playlist must report a different vid so the app drops
        # the cache and re-fetches (the "stale scene dropdown" fix).
        b1, _ = _bridge()  # scenes: Waveform / Plasma / Tunnel
        pl2 = _FakePlaylist("main", ["Waveform", "Plasma", "Tunnel", "Fire"])
        b2 = WledBridge(cast("list[tuple[str, Playlist]]", [("main", pl2)]), "c64cast")
        self.assertNotEqual(b1.info_dict()["vid"], b2.info_dict()["vid"])


# --- bridge writes (apply) --------------------------------------------------


class BridgeApplyTests(unittest.TestCase):
    def test_master_off_pauses(self):
        bridge, pl = _bridge()
        bridge.apply({"on": False})
        self.assertTrue(pl.pause_event.is_set())

    def test_bri_zero_dims_to_black_without_pausing(self):
        # Brightness is decoupled from transport: bri=0 dims fully to black
        # (user_dim=0) but must NOT pause — pausing is the Power (`on`) toggle's
        # job. (Regression: bri=0 used to pause, resetting the machine to BASIC
        # mid-drag — the "flashing cursor" HW bug.)
        bridge, pl = _bridge()
        bridge.apply({"bri": 0})
        self.assertFalse(pl.pause_event.is_set())
        self.assertEqual(pl.user_dim, 0.0)

    def test_master_on_resumes_when_paused(self):
        bridge, pl = _bridge()
        pl.pause_event.set()
        bridge.apply({"on": True, "bri": 128})
        self.assertTrue(pl.resume_event.is_set())

    def test_seg_fx_jumps(self):
        bridge, pl = _bridge()
        bridge.apply({"seg": [{"id": 0, "fx": 2}]})
        self.assertEqual(pl.jumps, [2])

    def test_seg_fx_out_of_range_ignored(self):
        bridge, pl = _bridge()
        bridge.apply({"seg": [{"id": 0, "fx": 99}]})
        self.assertEqual(pl.jumps, [])

    def test_seg_fx_to_current_scene_no_jump(self):
        # Selecting the already-current scene must not re-jump (it would restart
        # it) — matters for redundant re-selects and same-scene preset recall.
        bridge, pl = _bridge()  # index 0
        bridge.apply({"seg": [{"id": 0, "fx": 0}]})
        self.assertEqual(pl.jumps, [])

    def test_seg_off_pauses_that_system(self):
        bridge, pl = _bridge()
        bridge.apply({"seg": [{"id": 0, "on": False}]})
        self.assertTrue(pl.pause_event.is_set())

    def test_sx_slider_drives_live_param(self):
        src = _FakeSource()
        bridge, _pl = _bridge(source=src)
        bridge.apply({"seg": [{"id": 0, "sx": 255}]})  # full slider -> hi (10.0)
        self.assertAlmostEqual(src.speed, 10.0)
        bridge.apply({"seg": [{"id": 0, "sx": 0}]})  # zero -> lo (0.0)
        self.assertAlmostEqual(src.speed, 0.0)

    def test_slider_without_matching_param_is_noop(self):
        bridge, _pl = _bridge()  # no source declaring LIVE_PARAMS
        bridge.apply({"seg": [{"id": 0, "ix": 200}]})  # must not raise
        self.assertEqual(bridge.state_dict()["seg"][0]["ix"], 200)  # still echoed

    def test_ix_slider_drives_scope_gain_via_scene_prefix(self):
        # A scope scene has no source/effect holder, so the ix slider must fall
        # through _IX_TARGETS to `scene.gain` and drive the scene itself.
        bridge, pl = _bridge()
        scene = _FakeScopeScene()
        pl.current = cast("_FakeScene", scene)
        bridge.apply({"seg": [{"id": 0, "ix": 255}]})  # full slider -> hi (3.0)
        self.assertAlmostEqual(scene.gain, 3.0)
        bridge.apply({"seg": [{"id": 0, "ix": 0}]})  # zero -> lo (0.25)
        self.assertAlmostEqual(scene.gain, 0.25)

    def test_scene_prefix_resolves_via_set_live_param(self):
        # Direct check of the resolver's `scene.` case, mirroring
        # midi_control._apply_param's verbatim twin.
        from c64cast.wled_device import _set_live_param

        pl = _FakePlaylist("main", ["Waveform"])
        scene = _FakeScopeScene()
        pl.current = cast("_FakeScene", scene)
        _set_live_param(cast("Playlist", pl), ("source.scale", "scene.gain"), 128)
        # 128/255 of (0.25..3.0): 0.25 + (128/255)*2.75 ≈ 1.63
        self.assertAlmostEqual(scene.gain, 0.25 + (128 / 255.0) * 2.75, places=4)

    def test_echo_roundtrips_bri_and_pal(self):
        bridge, _ = _bridge()
        bridge.apply({"seg": [{"id": 0, "bri": 77, "pal": 3}]})
        seg = bridge.state_dict()["seg"][0]
        self.assertEqual(seg["bri"], 77)
        self.assertEqual(seg["pal"], 3)


# --- brightness -> real output dim ------------------------------------------


class BridgeBrightnessDimTests(unittest.TestCase):
    """A nonzero `bri` is a real dim: it lands on Playlist.user_dim + the live
    display mode's user_dim as (master/255)*(seg/255). bri==0 stays a pause and
    leaves user_dim intact so a later power-on restores at the prior brightness."""

    def test_master_bri_dims_playlist_and_mode(self):
        # Segment bri defaults to 255, so the master factor is isolated:
        # bri=128 -> 128/255 ≈ 0.502.
        mode = _FakeMode()
        bridge, pl = _bridge(display_mode=mode)
        bridge.apply({"bri": 128})
        self.assertAlmostEqual(pl.user_dim, 128 / 255, places=3)
        self.assertAlmostEqual(mode.user_dim, 128 / 255, places=3)

    def test_seg_bri_folds_with_master(self):
        mode = _FakeMode()
        bridge, pl = _bridge(display_mode=mode)
        bridge.apply({"bri": 255})  # master full first, isolates the seg factor
        bridge.apply({"seg": [{"id": 0, "bri": 128}]})
        self.assertAlmostEqual(pl.user_dim, 128 / 255, places=3)
        self.assertAlmostEqual(mode.user_dim, 128 / 255, places=3)

    def test_bri_zero_dims_black_and_does_not_pause(self):
        # bri=0 is a full dim to black (user_dim=0), decoupled from transport —
        # it must not pause. Power (`on`) is the only pause/resume control.
        mode = _FakeMode()
        bridge, pl = _bridge(display_mode=mode)
        bridge.apply({"bri": 200})  # establish a dim
        self.assertGreater(pl.user_dim, 0.0)
        bridge.apply({"bri": 0})  # slider to the bottom
        self.assertFalse(pl.pause_event.is_set())
        self.assertEqual(pl.user_dim, 0.0)
        self.assertEqual(mode.user_dim, 0.0)

    def test_power_off_pauses_without_touching_dim(self):
        # Transport is the Power toggle: `on=false` pauses and leaves user_dim
        # alone, so power-on resumes at the current brightness.
        mode = _FakeMode()
        bridge, pl = _bridge(display_mode=mode)
        bridge.apply({"bri": 200})
        prior = pl.user_dim
        bridge.apply({"on": False})
        self.assertTrue(pl.pause_event.is_set())
        self.assertEqual(pl.user_dim, prior)

    def test_top_level_bri_scales_every_system(self):
        # A master bri must re-dim every system, including one whose own seg bri
        # wasn't in this POST (it reads the echoed seg bri, default 255).
        plA = _FakePlaylist("A", ["s1", "s2"])
        plB = _FakePlaylist("B", ["s1", "s2"])
        systems = cast("list[tuple[str, Playlist]]", [("A", plA), ("B", plB)])
        bridge = WledBridge(systems, "cluster")
        bridge.apply({"bri": 64})
        self.assertAlmostEqual(plA.user_dim, 64 / 255, places=3)
        self.assertAlmostEqual(plB.user_dim, 64 / 255, places=3)


# --- palette / color live controls ------------------------------------------


class BridgePaletteColorTests(unittest.TestCase):
    def test_palettes_are_palette_modes(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.palettes(), ["Percell", "Cheap", "Vivid", "Grayscale"])

    def test_pal_swaps_palette_mode(self):
        mode = _FakeMode()
        bridge, _ = _bridge(display_mode=mode)
        bridge.apply({"seg": [{"id": 0, "pal": 2}]})  # index 2 -> "vivid"
        self.assertEqual(mode.palette_mode, "vivid")
        # Selecting a palette clears any force + turns the force toggle off.
        self.assertIsNone(mode.color_map)
        self.assertEqual(mode.palette_calls[-1], ("vivid", False))
        self.assertEqual(bridge.state_dict()["seg"][0]["pal"], 2)

    def test_pal_out_of_range_is_noop(self):
        mode = _FakeMode()
        bridge, _ = _bridge(display_mode=mode)
        bridge.apply({"seg": [{"id": 0, "pal": 99}]})
        self.assertEqual(mode.palette_calls, [])  # nothing applied
        self.assertEqual(bridge.state_dict()["seg"][0]["pal"], 99)  # still echoed

    def test_col_forces_palette_to_picked_colors(self):
        from c64cast import palette as pal

        mode = _FakeMode()
        mode.palette_mode = "grayscale"  # a non-percell mode
        bridge, _ = _bridge(display_mode=mode)
        # orange + cyan -> two distinct C64 indices, force toggle on.
        bridge.apply({"seg": [{"id": 0, "col": [[255, 160, 0], [0, 255, 255]]}]})
        assert isinstance(mode.color_map, pal.ColorMap)
        self.assertEqual(len(mode.color_map.indices), 2)
        # Forcing colors snaps to percell (the mode force_palette pairs with) so
        # grayscale's chromatic penalty can't wash the picked colors to gray.
        self.assertEqual(mode.palette_calls[-1], ("percell", True))
        self.assertEqual(bridge.state_dict()["seg"][0]["col"], [[255, 160, 0], [0, 255, 255]])

    def test_single_col_gets_a_contrast_partner(self):
        from c64cast import palette as pal

        mode = _FakeMode()
        bridge, _ = _bridge(display_mode=mode)
        bridge.apply({"seg": [{"id": 0, "col": [[255, 160, 0]]}]})  # one color
        assert isinstance(mode.color_map, pal.ColorMap)
        self.assertEqual(len(mode.color_map.indices), 2)  # partner added
        self.assertIn(0, mode.color_map.indices)  # black contrast partner

    def test_unchanged_col_does_not_clobber_palette_pick(self):
        # The WLED app re-POSTs the full segment (pal AND col) on every change.
        # A palette pick carrying the *same* col we already echoed must not let
        # that col re-apply its force and undo the palette. (Regression: HW.)
        mode = _FakeMode()
        bridge, _ = _bridge(display_mode=mode)
        # Establish a color force first.
        bridge.apply({"seg": [{"id": 0, "col": [[255, 160, 0], [0, 255, 255]]}]})
        forced_calls = len(mode.palette_calls)
        # Now the app changes only the palette, but echoes the unchanged col.
        bridge.apply({"seg": [{"id": 0, "pal": 2, "col": [[255, 160, 0], [0, 255, 255]]}]})
        # The palette change applied (vivid, force cleared)...
        self.assertEqual(mode.palette_mode, "vivid")
        # ...and the unchanged col did NOT re-trigger a force call after it.
        self.assertEqual(mode.palette_calls[forced_calls:], [("vivid", False)])

    def test_unchanged_pal_does_not_reapply(self):
        mode = _FakeMode()
        bridge, _ = _bridge(display_mode=mode)
        bridge.apply({"seg": [{"id": 0, "pal": 2}]})  # vivid
        n = len(mode.palette_calls)
        bridge.apply({"seg": [{"id": 0, "pal": 2}]})  # same again -> no-op
        self.assertEqual(len(mode.palette_calls), n)

    def test_segment_name_single_system_uses_device_name(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.state_dict()["seg"][0]["n"], "c64cast")

    def test_pal_and_col_noop_on_mode_without_setters(self):
        # A mode with no set_palette_mode/set_color_map (hires/petscii/blank):
        # both must be silent no-ops that still echo.
        class _BareMode:
            name = "hires"

        bridge, _ = _bridge(display_mode=_BareMode())
        bridge.apply({"seg": [{"id": 0, "pal": 1, "col": [[10, 20, 30]]}]})  # must not raise
        seg = bridge.state_dict()["seg"][0]
        self.assertEqual(seg["pal"], 1)
        self.assertEqual(seg["col"], [[10, 20, 30]])


# --- per-control capability hints (seg `c64` vendor key) --------------------


class SegCapsTests(unittest.TestCase):
    """`_seg_caps` mirrors the write-path applicability guards so the `/` page
    can gray out controls the current scene can't use. Rides each seg dict as a
    `c64` vendor key on both the /json poll and the WS push."""

    def test_no_scene_all_false(self):
        bridge, pl = _bridge()
        pl.current = None
        caps = bridge.state_dict()["seg"][0]["c64"]
        self.assertEqual(caps, {"pal": False, "col": False, "sx": False, "ix": False})

    def test_both_setters_make_pal_and_col_true(self):
        # A full mode (set_palette_mode + set_color_map, like MCM/MultiHires).
        mode = _FakeMode()
        bridge, _ = _bridge(display_mode=mode)
        caps = bridge.state_dict()["seg"][0]["c64"]
        self.assertTrue(caps["pal"])
        self.assertTrue(caps["col"])

    def test_bare_mode_pal_and_col_false(self):
        # hires/petscii/blank: a mode with neither setter.
        class _BareMode:
            name = "hires"

        bridge, _ = _bridge(display_mode=_BareMode())
        caps = bridge.state_dict()["seg"][0]["c64"]
        self.assertFalse(caps["pal"])
        self.assertFalse(caps["col"])

    def test_palette_only_mode_col_false(self):
        # set_palette_mode but no set_color_map: pal applies, col doesn't.
        class _PalOnlyMode:
            name = "mcm"

            def set_palette_mode(self, api, mode, *, force_palette=None):
                return mode

        bridge, _ = _bridge(display_mode=_PalOnlyMode())
        caps = bridge.state_dict()["seg"][0]["c64"]
        self.assertTrue(caps["pal"])
        self.assertFalse(caps["col"])

    def test_source_with_speed_sx_true_ix_false(self):
        # _FakeSource declares source.speed (an _SX_TARGET) and no ix target.
        bridge, _ = _bridge(source=_FakeSource())
        caps = bridge.state_dict()["seg"][0]["c64"]
        self.assertTrue(caps["sx"])
        self.assertFalse(caps["ix"])

    def test_scope_scene_gain_ix_true_sx_false(self):
        # scene.gain is an _IX_TARGET; a scope scene has no sx-side knob.
        bridge, pl = _bridge()
        pl.current = cast("_FakeScene", _FakeScopeScene())
        caps = bridge.state_dict()["seg"][0]["c64"]
        self.assertTrue(caps["ix"])
        self.assertFalse(caps["sx"])

    def test_caps_ride_full_and_present_per_segment(self):
        # The vendor key is on every seg of /json (full()) too, not just state.
        bridge, _ = _bridge(display_mode=_FakeMode())
        seg = bridge.full()["state"]["seg"][0]
        self.assertIn("c64", seg)
        self.assertEqual(set(seg["c64"]), {"pal", "col", "sx", "ix"})


# --- preset storage ---------------------------------------------------------


class PresetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # A not-yet-created subdir: the store must mkdir on first write.
        self.store = PresetStore(Path(self._tmp.name) / "sub" / "wled-x.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_reads_empty(self):
        self.assertEqual(self.store.all(), {})
        self.assertEqual(self.store.mtime_ns(), 0)

    def test_save_load_round_trip_persists_to_disk(self):
        self.store.save(1, {"n": "A", "seg": [{"id": 0, "fx": 2}]})
        self.assertEqual(self.store.all()["1"]["n"], "A")
        # A fresh instance reads the same file back (survives "restart").
        again = PresetStore(self.store.path)
        self.assertEqual(again.all()["1"]["seg"][0]["fx"], 2)

    def test_delete(self):
        self.store.save(1, {"n": "A"})
        self.store.save(2, {"n": "B"})
        self.store.delete(1)
        self.assertEqual(set(self.store.all()), {"2"})

    def test_id_zero_and_out_of_range_ignored(self):
        self.store.save(0, {"n": "reserved"})
        self.store.save(999, {"n": "too big"})
        self.assertEqual(self.store.all(), {})

    def test_next_free_id_fills_gaps(self):
        self.assertEqual(self.store.next_free_id(), 1)
        self.store.save(1, {})
        self.store.save(2, {})
        self.assertEqual(self.store.next_free_id(), 3)
        self.store.delete(1)
        self.assertEqual(self.store.next_free_id(), 1)

    def test_mtime_positive_and_nondecreasing(self):
        self.store.save(1, {"n": "A"})
        m1 = self.store.mtime_ns()
        self.assertGreater(m1, 0)
        time.sleep(0.01)
        self.store.save(2, {"n": "B"})
        self.assertGreaterEqual(self.store.mtime_ns(), m1)

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.save(1, {"n": "A"})
        self.assertEqual(list(self.store.path.parent.glob("*.tmp")), [])

    def test_corrupt_file_reads_empty(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.all(), {})


# --- bridge presets (save / recall / delete) --------------------------------


class BridgePresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bridge, self.pl = _bridge()
        # Redirect the store into a tempdir so tests never touch the repo's
        # gitignored presets/ dir.
        self.bridge._presets = PresetStore(Path(self._tmp.name) / "wled-test.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_presets_json_reserves_id_zero(self):
        self.assertEqual(self.bridge.presets_json(), {"0": {}})

    def test_psave_snapshots_current_state(self):
        self.pl.index = 2
        self.bridge.apply({"seg": [{"id": 0, "sx": 200, "ix": 50}]})
        self.bridge.apply({"psave": 1, "n": "Cool"})
        pj = self.bridge.presets_json()
        self.assertEqual(pj["1"]["n"], "Cool")
        seg = pj["1"]["seg"][0]
        self.assertEqual(seg["fx"], 2)
        self.assertEqual(seg["sx"], 200)
        self.assertEqual(seg["ix"], 50)
        # state.ps reflects the just-saved preset.
        self.assertEqual(self.bridge.state_dict()["ps"], 1)

    def test_psave_auto_picks_id_when_zero(self):
        self.bridge.apply({"psave": 0, "n": "Auto"})
        self.assertIn("1", self.bridge.presets_json())

    def test_psave_default_name_falls_back_to_scene_name(self):
        # No explicit `n` → the current scene's name (fake scene[0] = "Waveform").
        self.bridge.apply({"psave": 1})
        self.assertEqual(self.bridge.presets_json()["1"]["n"], "Waveform")

    def test_psave_default_name_uses_wled_label_for_random_pool(self):
        # No explicit `n` on a randomized-asset scene → the stable pool label, so
        # the preset doesn't promise the one tune that was loaded at save time.
        cast("_FakeScene", self.pl.current).wled_label = "SID: random pool"  # type: ignore[attr-defined]
        self.bridge.apply({"psave": 1})
        self.assertEqual(self.bridge.presets_json()["1"]["n"], "SID: random pool")

    def test_ps_recall_forces_unchanged_palette(self):
        # A preset storing pal=2 (no col). Recall must apply it EVEN WHEN the echo
        # already equals 2 — the only-when-changed guard is bypassed for recalls.
        mode = _FakeMode()
        scene = cast("_FakeScene", self.pl.current)
        scene.display_mode = mode
        scene.api = object()
        self.bridge._seg_echo[0]["pal"] = 2  # echo already matches → guard skips
        self.bridge._presets.save(
            1,
            {
                "n": "P",
                "on": True,
                "bri": 128,
                "seg": [{"id": 0, "fx": 0, "sx": 128, "ix": 128, "pal": 2}],
            },
        )
        mode.palette_calls.clear()
        self.bridge.apply({"ps": 1})
        # Forced apply happened despite the unchanged echo (vivid = PALETTE_MODES[2]).
        self.assertIn("vivid", [c[0] for c in mode.palette_calls])
        self.assertEqual(self.bridge.state_dict()["ps"], 1)

    def test_ps_resets_on_subsequent_manual_change(self):
        self.bridge._presets.save(
            1, {"n": "P", "on": True, "bri": 128, "seg": [{"id": 0, "fx": 0}]}
        )
        self.bridge.apply({"ps": 1})
        self.assertEqual(self.bridge.state_dict()["ps"], 1)
        self.bridge.apply({"bri": 200})  # manual change
        self.assertEqual(self.bridge.state_dict()["ps"], -1)

    def test_recall_missing_preset_is_noop(self):
        self.bridge.apply({"ps": 42})
        self.assertEqual(self.bridge.state_dict()["ps"], -1)
        self.assertEqual(self.pl.jumps, [])

    def test_ps_below_min_falls_through_as_manual(self):
        self.bridge._active_preset = 5
        self.bridge.apply({"ps": 0, "bri": 100})  # ps<=0 = "no preset"
        self.assertEqual(self.bridge.state_dict()["ps"], -1)
        self.assertEqual(self.bridge._global_bri, 100)

    def test_pdel_removes_and_clears_active(self):
        self.bridge._presets.save(
            3, {"n": "P", "on": True, "bri": 128, "seg": [{"id": 0, "fx": 0}]}
        )
        self.bridge.apply({"ps": 3})
        self.assertEqual(self.bridge.state_dict()["ps"], 3)
        self.bridge.apply({"pdel": 3})
        self.assertNotIn("3", self.bridge.presets_json())
        self.assertEqual(self.bridge.state_dict()["ps"], -1)


# --- HTTP + WS API ----------------------------------------------------------


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class WledApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self._tmp = tempfile.TemporaryDirectory()
        self.bridge, self.pl = _bridge()
        # Keep preset writes out of the repo's gitignored presets/ dir.
        self.bridge._presets = PresetStore(Path(self._tmp.name) / "wled-api.json")
        self.client = TestClient(build_wled_app(self.bridge))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_json(self):
        r = self.client.get("/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"state", "info", "effects", "palettes"})
        # The `c64` capability-hint vendor key rides each seg (load-bearing: real
        # WLED clients ignore unknown seg keys, but the payload must still parse).
        seg = r.json()["state"]["seg"][0]
        self.assertEqual(set(seg["c64"]), {"pal", "col", "sx", "ix"})

    def test_get_state_info_eff_pal_si(self):
        self.assertEqual(self.client.get("/json/state").json()["seg"][0]["id"], 0)
        self.assertEqual(self.client.get("/json/info").json()["product"], "c64cast")
        self.assertEqual(self.client.get("/json/eff").json(), ["Waveform", "Plasma", "Tunnel"])
        self.assertEqual(
            self.client.get("/json/pal").json(), ["Percell", "Cheap", "Vivid", "Grayscale"]
        )
        self.assertEqual(set(self.client.get("/json/si").json()), {"state", "info"})

    def test_get_index_serves_control_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("c64cast", r.text)
        self.assertIn("/json/state", r.text)
        # A single master Brightness slider (id=bri) drives top-level `bri` —
        # the same field the WLED app's own brightness slider uses, so they sync;
        # it's a real screen dim, not the old per-segment power-duplicate.
        self.assertIn("Brightness", r.text)
        self.assertIn('id="bri"', r.text)
        self.assertIn("post({bri:", r.text)
        self.assertIn("Palette", r.text)
        self.assertIn("'color'", r.text)  # picker.type = 'color'
        # Capability-hint disable logic: the page reads the seg `c64` key and
        # grays out controls the current scene can't use.
        self.assertIn("seg.c64", r.text)
        self.assertIn("cap-off", r.text)
        self.assertIn("markOff", r.text)
        # A scene pick blurs the <select> so the hints don't freeze behind the
        # focused-select render guard.
        self.assertIn("sel.blur()", r.text)
        # Live state now arrives over WebSocket (/ws), with a poll fallback.
        self.assertIn("new WebSocket(", r.text)
        self.assertIn("/ws", r.text)
        # Presets section: select + Apply / Save / Delete, wired to ps/psave/pdel.
        self.assertIn("Presets", r.text)
        self.assertIn('id="presetSel"', r.text)
        self.assertIn("applyPreset", r.text)
        self.assertIn("psave", r.text)
        self.assertIn("pdel", r.text)

    def test_get_description_xml(self):
        r = self.client.get("/description.xml")
        self.assertEqual(r.status_code, 200)
        self.assertIn("xml", r.headers["content-type"])
        self.assertIn("<friendlyName>c64cast</friendlyName>", r.text)
        self.assertIn(f"<UDN>uuid:{self.bridge.device_uuid()}</UDN>", r.text)
        self.assertIn(f"<serialNumber>{self.bridge.mac()}</serialNumber>", r.text)

    def test_post_state_pauses(self):
        r = self.client.post("/json/state", json={"on": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertTrue(self.pl.pause_event.is_set())

    def test_post_json_jumps(self):
        self.client.post("/json", json={"seg": [{"id": 0, "fx": 1}]})
        self.assertEqual(self.pl.jumps, [1])

    def test_presets_json_empty_reserves_id_zero(self):
        self.assertEqual(self.client.get("/presets.json").json(), {"0": {}})

    def test_psave_then_get_reflects_and_pmt_bumps(self):
        pmt0 = self.client.get("/json/info").json()["fs"]["pmt"]
        self.client.post("/json/state", json={"psave": 1, "n": "Fire look"})
        pj = self.client.get("/presets.json").json()
        self.assertEqual(pj["1"]["n"], "Fire look")
        pmt1 = self.client.get("/json/info").json()["fs"]["pmt"]
        self.assertGreater(pmt1, pmt0)

    def test_ps_recall_jumps_back_via_post(self):
        self.client.post("/json/state", json={"psave": 1, "n": "Home"})  # fx=0
        self.pl.index = 2
        self.client.post("/json/state", json={"ps": 1})
        self.assertEqual(self.pl.jumps, [0])
        self.assertEqual(self.client.get("/json/state").json()["ps"], 1)

    def test_ws_sends_state_on_connect_and_applies(self):
        with self.client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            self.assertIn("state", hello)
            self.assertIn("info", hello)
            # The proactive WS push carries the caps key too (refreshes on
            # auto-advance for free).
            self.assertIn("c64", hello["state"]["seg"][0])
            ws.send_json({"seg": [{"id": 0, "fx": 2}]})
            update = ws.receive_json()
            self.assertIn("state", update)
        self.assertEqual(self.pl.jumps, [2])


if __name__ == "__main__":
    unittest.main()
