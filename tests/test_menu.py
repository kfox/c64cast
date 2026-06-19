"""Tests for the on-C64 MenuOverlay: context-sensitive option model, nav
state machine, live-apply wiring, save flow, and mode-dispatched rendering."""

from __future__ import annotations

import unittest
from typing import cast

from _fakes import FakeAPI

from c64cast.backend import C64Backend
from c64cast.c64 import KEYBUF, SCREEN
from c64cast.config import SceneCfg
from c64cast.overlays.menu import MenuItem, MenuOverlay, build_menu_items, can_show_menu
from c64cast.scenes import Scene


class BitmapMode:
    """mhires-like display: supports set_palette_mode, no set_style."""

    name = "mhires"
    use_reu_staged = False

    def __init__(self):
        self.palette_mode = "percell"
        self.calls: list[str] = []

    def set_palette_mode(self, api, v, *, force_palette=None):
        self.palette_mode = v
        self.calls.append(v)
        return f"palette_mode={v}"


class PetsciiMode:
    """petscii-like display: supports set_style, no set_palette_mode."""

    name = "petscii"

    def __init__(self):
        self.style = "default"
        self.calls: list[str] = []

    def set_style(self, api, v):
        self.style = v
        self.calls.append(v)
        return f"style={v}"


class FakeScene:
    def __init__(self, cfg, mode, *, duration_s=20.0, target_fps=None):
        self._cfg = cfg
        self.display_mode = mode
        self.overlays: list = []
        self.duration_s = duration_s
        self.target_fps = target_fps


def _api():
    return cast(C64Backend, FakeAPI())


def _overlay(scene, *, can_save=True, prompt_to_save=True, save_fn=None):
    return MenuOverlay(
        cast(Scene, scene),
        _api(),
        can_save=can_save,
        prompt_to_save=prompt_to_save,
        save_fn=save_fn or (lambda: True),
    )


def _items(scene):
    return build_menu_items(cast(Scene, scene), _api())


class OptionModelTest(unittest.TestCase):
    def test_generative_mhires_offers_palette_not_style(self):
        cfg = SceneCfg(type="generative", display="mhires", source="plasma")
        scene = FakeScene(cfg, BitmapMode())
        header, items = _items(scene)
        labels = [it.label for it in items]
        self.assertIn("PALETTE", labels)
        self.assertNotIn("STYLE", labels)  # mhires has no set_style
        self.assertIn("DURATION", labels)
        self.assertIn("FPS", labels)
        self.assertTrue(any("generative" in h for h in header))

    def test_petscii_offers_style_not_palette(self):
        cfg = SceneCfg(type="webcam", display="petscii")
        scene = FakeScene(cfg, PetsciiMode())
        _header, items = _items(scene)
        labels = [it.label for it in items]
        self.assertIn("STYLE", labels)
        self.assertNotIn("PALETTE", labels)  # petscii has no set_palette_mode

    def test_can_show_menu_gates_on_display(self):
        self.assertTrue(
            can_show_menu(cast(Scene, FakeScene(SceneCfg(type="webcam"), PetsciiMode())))
        )
        self.assertTrue(
            can_show_menu(cast(Scene, FakeScene(SceneCfg(type="webcam"), BitmapMode())))
        )

        class McmMode:
            name = "mcm"

        self.assertFalse(can_show_menu(cast(Scene, FakeScene(SceneCfg(type="webcam"), McmMode()))))


class LiveApplyTest(unittest.TestCase):
    def test_palette_change_updates_cfg_and_mode(self):
        cfg = SceneCfg(type="generative", display="mhires", source="plasma")
        mode = BitmapMode()
        scene = FakeScene(cfg, mode)
        _h, items = _items(scene)
        pal = next(it for it in items if it.label == "PALETTE")
        self.assertEqual(pal.get(), "percell")
        pal.change(+1)  # percell → cheap (next in choices)
        self.assertEqual(cfg.palette_mode, mode.palette_mode)  # cfg + live in lockstep
        self.assertEqual(mode.calls[-1], mode.palette_mode)
        self.assertNotEqual(mode.palette_mode, "percell")

    def test_enum_cycle_wraps_both_directions(self):
        choices = ("a", "b", "c")
        box = {"v": "a"}
        it = MenuItem(
            "X",
            "enum",
            get=lambda: box["v"],
            set=lambda v: box.__setitem__("v", v),
            choices=choices,
        )
        it.change(-1)  # a → c (wrap)
        self.assertEqual(box["v"], "c")
        it.change(+1)  # c → a (wrap)
        self.assertEqual(box["v"], "a")

    def test_numeric_step_and_none_default(self):
        box = {"v": None}
        it = MenuItem(
            "FPS",
            "int",
            get=lambda: box["v"],
            set=lambda v: box.__setitem__("v", v),
            step=5.0,
            minimum=1.0,
            default_when_none=30.0,
        )
        it.change(+1)  # None → 30 base + 5
        self.assertEqual(box["v"], 35)
        self.assertIsInstance(box["v"], int)

    def test_numeric_clamps_to_minimum(self):
        box = {"v": 3.0}
        it = MenuItem(
            "D",
            "float",
            get=lambda: box["v"],
            set=lambda v: box.__setitem__("v", v),
            step=5.0,
            minimum=1.0,
        )
        it.change(-1)  # 3 - 5 = -2 → clamp to 1
        self.assertEqual(box["v"], 1.0)


class NavTest(unittest.TestCase):
    def _scene(self):
        cfg = SceneCfg(type="generative", display="mhires", source="plasma")
        return FakeScene(cfg, BitmapMode())

    def test_crsr_down_up_moves_selection(self):
        # Direction rides on the decoded code: CRSR-down = next, CRSR-up
        # (kernal's SHIFT+CRSR-down decode) = previous, both wrap.
        ov = _overlay(self._scene())
        n = len(ov.items)
        self.assertGreater(n, 1)
        ov.on_key(KEYBUF.CRSR_DOWN)
        self.assertEqual(ov.sel, 1)
        ov.on_key(KEYBUF.CRSR_UP)  # reverse
        self.assertEqual(ov.sel, 0)
        ov.on_key(KEYBUF.CRSR_UP)  # wrap to last
        self.assertEqual(ov.sel, n - 1)

    def test_crsr_right_changes_value_and_marks_dirty(self):
        scene = self._scene()
        ov = _overlay(scene)
        ov.sel = next(i for i, it in enumerate(ov.items) if it.label == "PALETTE")
        self.assertFalse(ov.dirty)
        ov.on_key(KEYBUF.CRSR_RIGHT)
        self.assertTrue(ov.dirty)
        self.assertNotEqual(scene.display_mode.palette_mode, "percell")

    def test_crsr_left_reverses_value_change(self):
        # CRSR-left (kernal's SHIFT+CRSR-right decode) steps the value the
        # opposite way from CRSR-right.
        scene = self._scene()
        ov = _overlay(scene)
        ov.sel = next(i for i, it in enumerate(ov.items) if it.label == "PALETTE")
        ov.on_key(KEYBUF.CRSR_RIGHT)
        forward = scene.display_mode.palette_mode
        ov.on_key(KEYBUF.CRSR_LEFT)
        self.assertEqual(scene.display_mode.palette_mode, "percell")
        self.assertNotEqual(forward, "percell")


class CloseAndSaveTest(unittest.TestCase):
    def _scene(self):
        return FakeScene(
            SceneCfg(type="generative", display="mhires", source="plasma"), BitmapMode()
        )

    def test_toggle_clean_closes_immediately(self):
        ov = _overlay(self._scene())
        self.assertTrue(ov.on_toggle())
        self.assertTrue(ov.closed)
        self.assertEqual(ov.state, "browse")

    def test_toggle_dirty_enters_confirm_then_saves_on_return(self):
        calls = []
        ov = _overlay(self._scene(), save_fn=lambda: (calls.append(1), True)[1])
        ov.dirty = True
        closed = ov.on_toggle()
        self.assertFalse(closed)
        self.assertEqual(ov.state, "confirm")
        self.assertFalse(ov.closed)
        ov.on_key(KEYBUF.RETURN)  # YES
        self.assertEqual(calls, [1])
        self.assertTrue(ov.closed)

    def test_confirm_non_return_discards_without_saving(self):
        calls = []
        ov = _overlay(self._scene(), save_fn=lambda: (calls.append(1), True)[1])
        ov.dirty = True
        ov.on_toggle()
        self.assertEqual(ov.state, "confirm")
        ov.on_key(KEYBUF.CRSR_DOWN)  # anything but RETURN = discard
        self.assertEqual(calls, [])
        self.assertTrue(ov.closed)

    def test_prompt_to_save_false_closes_without_confirm(self):
        calls = []
        ov = _overlay(
            self._scene(), prompt_to_save=False, save_fn=lambda: (calls.append(1), True)[1]
        )
        ov.dirty = True
        self.assertTrue(ov.on_toggle())
        self.assertTrue(ov.closed)
        self.assertEqual(ov.state, "browse")
        self.assertEqual(calls, [])

    def test_cannot_save_closes_without_confirm(self):
        calls = []
        ov = _overlay(self._scene(), can_save=False, save_fn=lambda: (calls.append(1), True)[1])
        ov.dirty = True
        self.assertTrue(ov.on_toggle())
        self.assertTrue(ov.closed)
        self.assertEqual(calls, [])


class RenderTest(unittest.TestCase):
    def test_char_scene_writes_screen_and_color(self):
        scene = FakeScene(SceneCfg(type="webcam", display="petscii"), PetsciiMode())
        api = FakeAPI()
        ov = MenuOverlay(
            cast(Scene, scene),
            cast(C64Backend, api),
            can_save=True,
            prompt_to_save=True,
            save_fn=lambda: True,
        )
        ov.process_frame(cast(C64Backend, api), cast(Scene, scene), 0.0)
        # Wrote into screen RAM ($0400+) and color RAM ($D800+) ranges.
        self.assertTrue(any(SCREEN.RAM <= a < SCREEN.RAM + 1000 for a in api.regions))
        self.assertTrue(any(SCREEN.COLOR_RAM <= a < SCREEN.COLOR_RAM + 1000 for a in api.regions))

    def test_bitmap_scene_writes_bitmap(self):
        scene = FakeScene(
            SceneCfg(type="generative", display="mhires", source="plasma"), BitmapMode()
        )
        api = FakeAPI()
        ov = MenuOverlay(
            cast(Scene, scene),
            cast(C64Backend, api),
            can_save=True,
            prompt_to_save=True,
            save_fn=lambda: True,
        )
        ov.process_frame(cast(C64Backend, api), cast(Scene, scene), 0.0)
        self.assertTrue(any(SCREEN.BITMAP <= a < SCREEN.BITMAP + 8000 for a in api.regions))

    def test_staged_bitmap_skips_panel(self):
        mode = BitmapMode()
        mode.use_reu_staged = True
        scene = FakeScene(SceneCfg(type="generative", display="mhires", source="plasma"), mode)
        api = FakeAPI()
        ov = MenuOverlay(
            cast(Scene, scene),
            cast(C64Backend, api),
            can_save=True,
            prompt_to_save=True,
            save_fn=lambda: True,
        )
        ov.process_frame(cast(C64Backend, api), cast(Scene, scene), 0.0)
        self.assertEqual(api.regions, {}, "staged bitmap scene draws no panel (preview only)")

    def test_panel_repaints_every_frame_over_dynamic_scene(self):
        # Regression: a scene that re-renders the panel's addresses every frame
        # (e.g. generative plasma) would clobber the panel, and the panel's own
        # per-region cache would then treat its unchanged content as a no-op
        # and skip the repaint — leaving the menu invisible after frame 1. The
        # overlay must invalidate its regions so write_region re-pushes each
        # frame. This fake models BufferedWriteBackend's per-region skip.
        class SkipCacheAPI(FakeAPI):
            def __init__(self):
                super().__init__()
                self._region_cache: dict[int, bytes] = {}
                self.region_pushes: dict[int, int] = {}

            def write_region(self, addr, data, region_id=None, full_threshold=0.6):
                key = region_id if region_id is not None else addr
                b = bytes(data)
                if self._region_cache.get(key) == b:
                    return 0  # unchanged → skip, like the real cache
                self._region_cache[key] = b
                self.region_pushes[key] = self.region_pushes.get(key, 0) + 1
                return len(b)

            def invalidate_region(self, region_id):
                super().invalidate_region(region_id)
                self._region_cache.pop(region_id, None)

        api = SkipCacheAPI()
        scene = FakeScene(
            SceneCfg(type="generative", display="mhires", source="plasma"), BitmapMode()
        )
        ov = MenuOverlay(
            cast(Scene, scene),
            cast(C64Backend, api),
            can_save=False,
            prompt_to_save=False,
            save_fn=lambda: True,
        )
        ov.process_frame(cast(C64Backend, api), cast(Scene, scene), 0.0)
        self.assertTrue(api.region_pushes, "panel pushed something on frame 1")
        regions_frame1 = set(api.region_pushes)
        # Frame 2 with identical panel content: every region must push AGAIN.
        ov.process_frame(cast(C64Backend, api), cast(Scene, scene), 0.1)
        for region in regions_frame1:
            self.assertGreaterEqual(
                api.region_pushes[region], 2, f"region {region} not repainted on frame 2"
            )


if __name__ == "__main__":
    unittest.main()
