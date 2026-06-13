# Extending c64cast

This guide covers the four pluggable surfaces — **Overlays**, **Scenes**,
**DisplayModes**, and interstitial **Backgrounds** — plus the testing
patterns that keep the suite hardware-free. For runtime behavior see
[CLAUDE.md](../CLAUDE.md); for end-user config see [usage.md](usage.md).

## Conventions

* All bytes destined for the U64 go through `Ultimate64API.write_region`
  (delta-cached, region-keyed) or `write_memory_file` (raw bulk). Never
  open a `requests` session yourself — you'd bypass the async queue and
  the dirty cache.
* Screen-code (not PETSCII) for anything that writes to $0400. Use
  `overlays.ascii_to_screen()` for the common ASCII case.
* C64 color names come from `palette.C64_COLORS` (`"yellow"`,
  `"light green"`, etc.). Accept the string; look up the byte.
* Type-annotate (Python 3.11+, `from __future__ import annotations`).
  The codebase is gradually-typed; new code should keep up.
* No emojis, no comments that just restate the code. Keep doc comments
  to the *why*.

## Adding an Overlay

Overlays are the most common extension point — drop a file into
[c64cast/overlays/](../c64cast/overlays/), register it under a
config name, implement three methods.

### Minimal example

```python
# c64cast/overlays/blink.py
"""Blink the border color between two C64 colors at a configurable rate."""
from __future__ import annotations

from ..palette import C64_COLORS
from . import Overlay, register


@register("blink")
class BlinkOverlay(Overlay):
    REQUIRES_PETSCII = False          # bitmap-safe; only touches $D020
    REQUIRES_AUDIO = False

    def __init__(self, color_a: str = "black", color_b: str = "white",
                 hz: float = 1.0):
        self.color_a = C64_COLORS[color_a]
        self.color_b = C64_COLORS[color_b]
        self.hz = float(hz)
        self._last_state = None

    def setup(self, api, scene):
        self._last_state = None

    def process_frame(self, api, scene, t):
        state = int(t * 2 * self.hz) & 1
        if state == self._last_state:
            return                          # no traffic if nothing changed
        api.write_regs("D020", self.color_b if state else self.color_a)
        self._last_state = state

    def teardown(self, api, scene):
        api.write_regs("D020", C64_COLORS["black"])
```

Then register it in the loader's auto-import block (otherwise the
`@register` decorator won't run and the loader can't find it):

```python
# c64cast/overlays/__init__.py — inside _load_all()
from . import (
    ...,
    blink,        # <-- add this
    ...
)
```

Use it from config:

```toml
[[scenes.overlays]]
type = "blink"
color_a = "black"
color_b = "yellow"
hz = 2.0
```

### Restriction flags

* `REQUIRES_PETSCII` — true when the overlay paints PETSCII codes to
  $0400 / $D800. Runs on any display mode with `is_petscii_compatible
  = True` (currently `petscii` and `blank`).
* `COMPATIBLE_MODES` — tuple of display-mode names this overlay
  supports. Empty (default) = no restriction. Used when the overlay
  isn't a clean fit for the PETSCII/bitmap split — e.g. `big_text`
  whitelists `("blank", "mcm")`.
* `REQUIRES_AUDIO` — true when `process_frame` reads
  `audio.get_recent_samples()`. Loader raises with a clear error.

These are validated by `overlays.validate_for_scene` (invoked from
`config._attach_overlays` in [config.py](../c64cast/config.py)) at
config-load time, not at the first frame.

### Painting into screen / color RAM (`compose`)

The `blink` example above pokes a VIC register directly from
`process_frame()`, which is the right path for register writes. But an
overlay that paints **PETSCII screen codes** to `$0400` / `$D800` should
**not** use `process_frame` + `write_region` — it would race the scene's
own screen write and flicker. Instead, set `PAINTS_INTO_BUFFERS = True`
and implement `compose()`:

```python
def compose(self, buffers, scene, t):
    # buffers["screen"] and buffers["color"] are uint8 numpy arrays of
    # length 1000 (40×25 cells). Mutate them in place; the scene uploads
    # the composed result once per frame.
    buffers["screen"][row * 40 + col] = some_screen_code
    buffers["color"][row * 40 + col] = C64_COLORS["white"]
```

The Playlist **skips** `process_frame()` for `PAINTS_INTO_BUFFERS`
overlays — the scene invokes `compose()` on each attached overlay during
its render path, so scene + overlays produce a single composed frame
that's pushed in one upload. `compose()` is only called when the scene's
display mode supports it (PETSCII / blank). See `corner_text.py` and
`marquee.py` for working examples. There is no region-ID allocation to
do: buffer-painting overlays write into the shared `screen`/`color`
arrays, not via per-overlay `write_region` slots.

### Reusing shared bases

* **Single-line corner text** (clock-style) → subclass
  `overlays.corner_text.CornerTextOverlay` and implement
  `compute_strings(t)`. You get change-detection and shrink-blanking for
  free.
* **Ticker text** (single line, scrolling) → subclass
  `overlays.marquee.MarqueeBase` and implement `_current_text()`. The
  RSS overlay is a one-page example of this pattern.

### Testing an overlay

Tests use a `FakeAPI` (in [tests/_fakes.py](../tests/_fakes.py)) and
`FakeAudio` (in [tests/test_overlays.py](../tests/test_overlays.py)) — no
hardware involved. Pattern:

```python
def test_blink_emits_on_state_change(self):
    api = FakeAPI()
    overlay = build_overlay({"type": "blink", "hz": 1.0}, audio=None)
    overlay.setup(api, scene=None)
    overlay.process_frame(api, scene=None, t=0.0)
    self.assertEqual(api.regs["D020"], (C64_COLORS["black"],))
    overlay.process_frame(api, scene=None, t=0.51)
    self.assertEqual(api.regs["D020"], (C64_COLORS["white"],))
```

Hit at minimum: registry lookup (`build_overlay` returns the right
class), restriction validation (`validate_for_scene` raises when
appropriate), and one positive-path render.

## Adding a Scene

Scenes are bigger lifts than overlays — a Scene is responsible for
producing one frame of *content* per `process_frame()` call (the
overlays paint over it). Most users won't need to add scenes; if you do,
subclass `Scene` from [scenes.py](../c64cast/scenes.py).

### Skeleton

```python
# c64cast/scenes.py (or your own module)
class MyScene(Scene):
    def __init__(self, api, audio, display_mode, name="My scene"):
        super().__init__(api, audio, display_mode, name)
        self.target_fps = 30.0   # only set if your scene can't sustain system rate

    def setup(self):
        super().setup()
        self.display_mode.setup(self.api)
        # Allocate any per-scene resources here (threads, hardware regs).

    def process_frame(self, current_time: float) -> bool:
        frame_bgr = self._produce_frame()    # whatever your source is
        cropped = _crop_to_aspect(frame_bgr)
        self.display_mode.render(self.api, cropped)
        # Don't run overlays — the Playlist does that for you.
        return True                          # False = scene is finished

    def teardown(self):
        super().teardown()
        # Stop threads, restore hardware state, etc.
```

### Wire it into the config loader

Open [config.py](../c64cast/config.py) and add a branch in
`scenes_from_config`:

```python
elif s.type == "my_scene":
    mode = _build_display_mode(s.display)
    scene = MyScene(api, audio, mode, s.name or "My scene")
```

Then add any custom config fields to `SceneCfg` (also in `config.py`)
so they round-trip through TOML.

### Things to honor

* `target_fps` — set in `__init__` if your scene can't sustain the
  Playlist default (60 / 50).
* `is_done` — set to True if your scene wants to advance early (the
  CTRL-skip path does this externally; you can too).
* `audio` may be None — guard `audio.get_recent_samples()` etc.
* `self.api.invalidate_cache()` if you change the meaning of any cached
  region (mode switches do this for you automatically via
  `DisplayMode.setup`).

The Playlist wraps your `setup`/`process_frame`/`teardown` calls with
overlay calls; your scene doesn't need to know overlays exist.

## Adding a DisplayMode

Display modes are how a frame becomes VIC bytes. Adding one is rare;
when needed, subclass `DisplayMode` from
[modes.py](../c64cast/modes.py):

```python
class MyDisplayMode(DisplayMode):
    name = "mymode"
    is_bitmapped = False     # True if you write $2000 instead of $0400/$D800

    def setup(self, api):
        super().setup(api)                # drops the delta cache for you
        # Put the VIC into the right mode: bank, $D011, $D016, $D018, etc.
        api.write_memory("d018", "...")

    def render(self, api, frame_bgr):
        # frame_bgr is a (H, W, 3) uint8 in OpenCV BGR order.
        # Quantize to whatever your mode needs, then push:
        api.write_region(0x0400, screen_bytes, region_id=REG_SCREEN)
        api.write_region(0xD800, color_bytes,  region_id=REG_COLOR)
```

Wire it into the loader's mode factory:

```python
# config.py — _build_display_mode
if name == "mymode":
    return MyDisplayMode()
```

Performance tips (these are what makes the bundled modes hit 30+ fps):

* Use `palette.quantize_distances()` for nearest-color matching — it
  uses the `(x-p)²` distance expansion and avoids the naive (N, 16, 3)
  broadcast tensor.
* Reuse one distance matrix across multiple per-cell decisions if your
  mode has nested searches (see how MCM does it).
* Replace Python loops with `np.argmin` / fancy indexing.
* `write_region` only sends the diff — let the delta cache do its job.
  Don't write your own diff layer on top.

## Adding an Interstitial Background

Backgrounds are the parallax decoration that plays between scenes
(during the "UP NEXT: …" interstitial). Add one in
[backgrounds.py](../c64cast/backgrounds.py):

```python
@register("mybg")                  # decorator sets cls.name + adds to REGISTRY
class MyBackground(Background):
    def _fill(self, chars, colors, t, rows, bg_color):
        # chars and colors are uint8 arrays of length 1000 (40×25), already
        # filled with SC_SPACE / bg_color. Only paint cells inside `rows`.
        for y in rows:
            for x in range(40):
                idx = y * 40 + x
                chars[idx]  = SC_FULL
                colors[idx] = C64_COLORS["light blue"]
```

The `@register("mybg")` decorator (mirroring the overlay pattern) sets
the class `name` and adds it to `REGISTRY` for you — no manual dict edit.

Use it from the `[interstitial]` config:

```toml
[interstitial]
background = "mybg"             # or "random" to mix yours in
```

`"random"` rotation excludes `"none"` automatically; your new
background will be one of the random picks.

## Adding a CLI flag

The pattern in [cli.py](../c64cast/cli.py) is "argparse `default=None`,
plus an entry in `CLI_TO_CFG`":

```python
# cli.py — inside _parser()
g_audio.add_argument(
    "--my-knob",
    type=float, default=None,
    help="What it does (must be 0.0-1.0).",
)
```

```python
# config.py — CLI_TO_CFG
"my_knob": ("audio", "my_knob"),
```

The merge function (`merge_cli`) skips fields where the CLI value is
`None`, so absence is distinguishable from "user passed the default."
Don't break that — never use a non-None argparse default for an
override-able flag, or you'll permanently shadow the TOML value.

## Adding a control-plane endpoint

The FastAPI app in
[control_plane.py](../c64cast/control_plane.py) speaks to the
`Playlist` via threading.Events. To add an action:

1. Add an `Event` to `Playlist.__init__` and a handler in the run loop.
2. Register an endpoint:

   ```python
   @app.post("/freeze")
   def freeze():
       playlist.freeze_event.set()
       return {"ok": True}
   ```

3. If the action should also be triggerable from the C64 keyboard,
   extend [keyboard.py](../c64cast/keyboard.py) (you'd need a new
   modifier-key edge or a chord).

Keep `pause` / `resume` / `skip` / `reload` semantics — they're the
documented surface.

## Testing patterns

The suite (`python -m unittest discover tests`) runs in CI with no
hardware. To keep it that way:

* **`FakeAPI`** records every `write_memory*` / `write_region` /
  `write_regs` call. Assert on `api.regions[addr]` or `api.regs[addr]`.
* **`FakeAudio`** hands a pre-canned numpy array back from
  `get_recent_samples()` so FFT-based overlays test deterministically.
* **No `time.sleep` in tests** — pass a deterministic `t` into
  `process_frame(api, scene, t)` instead.
* **No `requests`** — patch with `unittest.mock.patch('requests.get',
  ...)` if you must touch HTTP code paths (the weather and RSS overlays
  do this).
* **No background threads** — most overlays that start a thread in
  `setup()` are tested in a "skip the thread, call the fetch function
  directly" mode. Mirror that pattern.

A good new test file looks like
[tests/test_overlays.py](../tests/test_overlays.py): one `unittest.TestCase`
per surface, fakes at the top, three-to-six small `test_*` methods.

## Where things live (cheat sheet)

| What you're adding         | Where it goes                                                | Wire-up                                                                 |
|----------------------------|--------------------------------------------------------------|-------------------------------------------------------------------------|
| Overlay                    | [c64cast/overlays/yours.py](../c64cast/overlays/)        | `@register("yours")` + add to `_load_all()` in `overlays/__init__.py`   |
| Scene                      | [c64cast/scenes.py](../c64cast/scenes.py) (or new file)  | branch in `config.scenes_from_config` + optional `SceneCfg` fields      |
| DisplayMode                | [c64cast/modes.py](../c64cast/modes.py)                  | branch in `config._build_display_mode`                                  |
| Background                 | [c64cast/backgrounds.py](../c64cast/backgrounds.py)      | `@register("yours")` decorator                                          |
| CLI flag                   | [c64cast/cli.py](../c64cast/cli.py)                      | `default=None` + entry in `config.CLI_TO_CFG`                           |
| Control-plane endpoint     | [c64cast/control_plane.py](../c64cast/control_plane.py)  | new event on `Playlist` + handler in the run loop                       |
| Test                       | [tests/test_*.py](../tests/)                                 | `FakeAPI` (`tests/_fakes.py`) + `FakeAudio` (`tests/test_overlays.py`) reusable |
