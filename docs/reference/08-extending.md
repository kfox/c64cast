---
number: 7
---

# Extending c64cast

Every other chapter in this book is for somebody using c64cast. This one is for
somebody changing it: adding a scene type, an overlay, a generator or an effect
to the program itself, rather than configuring the ones that ship with it.

It is deliberately short. The surfaces are small — a scene is four methods, an
overlay three, a generator one — and what makes them small is that the playlist
handles setup order, overlay composition, pacing, fades and teardown, so none of
those pieces has to. What follows is the shape of each surface and the handful
of rules that are not obvious from it.

`docs/architecture.md` and the notes it indexes are the other half of this. They
explain why each module is built the way it is, including the approaches that
were tried and abandoned — which is what you want before changing one, and not
what you want while writing your first scene.

## Writing Your Own Scene

A scene produces one frame of *content* per call. The playlist wraps it with
setup, overlays, pacing and teardown, so a scene never needs to know that
overlays exist.

```python
class MyScene(Scene):
    def __init__(self, api, audio, display_mode, name="My scene"):
        super().__init__(api, audio, display_mode, name)
        self.target_fps = 30.0        # only if it can't sustain system rate

    def setup(self):
        super().setup()
        self.display_mode.setup(self.api)

    def process_frame(self, current_time: float) -> bool:
        frame_bgr = self._produce_frame()
        self.display_mode.render(self.api, _crop_to_aspect(frame_bgr))
        return True                   # False means finished

    def teardown(self):
        super().teardown()
```

Then add a branch to the configuration loader's scene factory, and any fields
the scene takes to the scene dataclass, so they round-trip through TOML.

Four things to honour:

- **`audio` may be `None`.** It is `None` whenever audio is off, the scene sets
  `audio = false`, or another system in an ensemble holds the audio slot.
- **Return `False` when finished,** or set `is_done`. The skip path sets it
  externally; you may too.
- **Every byte goes through the region-cached write calls.** Opening your own
  HTTP session bypasses both the shared connection's mutex and the dirty cache,
  and the DMA service accepts one connection.
- **Invalidate the cache** if you change what a cached region means. A display
  mode's setup does that for you.

Set `target_fps` only when the scene genuinely cannot sustain the system rate.
The defaults already account for the link, and a scene that pins a low rate for
no reason simply looks worse. Chapter 5 is what those defaults are reasoning
about.

## Writing Your Own Overlay or Generator

Both are small, and both are registered by a decorator rather than by editing a
table.

### An Overlay

Three methods, plus the class attributes that declare where it may run:

```python
@register("blink")
class BlinkOverlay(Overlay):
    REQUIRES_PETSCII = False        # only touches $D020
    REQUIRES_AUDIO = False

    def setup(self, api, scene): ...
    def process_frame(self, api, scene, t): ...
    def teardown(self, api, scene): ...
```

| Attribute | Meaning |
|---|---|
| `REQUIRES_PETSCII` | It writes PETSCII codes to screen and colour RAM, so it needs a character mode |
| `COMPATIBLE_MODES` | An explicit whitelist, for an overlay that is not a clean fit for that split |
| `REQUIRES_AUDIO` | It cannot work at all without the audio streamer; refused at load when audio is off |
| `WANTS_AUDIO` | It uses the streamer when there is one and has a fallback; never refused |

Appendix D's compatibility matrix is built from those, and they are checked when
the configuration loads rather than when the overlay would first draw.

**An overlay that paints characters should not write them itself.** Setting
`PAINTS_INTO_BUFFERS = True` and implementing `compose(buffers, scene, t)` gets
its glyphs folded into the scene's own frame, so scene and overlays go out as
one upload. Writing screen memory from `process_frame` instead races the scene's
own write and flickers. A register write — a border colour, say — is the case
where `process_frame` is the right method.

Two base classes cover most of what people write: one for single-line corner
text, which brings change detection with it, and one for a scrolling ticker.

### A Generator or an Effect

A generator renders 320×200 and returns it; an effect takes a frame and returns
a frame. Both declare their live-tunable parameters as one class attribute:

```python
LIVE_PARAMS = {"speed": (0.1, 4.0), "scale": (0.5, 8.0)}
```

That line is the whole wiring. It puts the parameter in Appendix F, on a MIDI
knob, in the web console's effect rack, and under the WLED sliders, with nothing
else to register. A discrete choice rather than a number goes in `LIVE_CHOICES`,
as a tuple of the values it accepts.

Only declare **independent single-numeric fields** there. A live write is one
attribute assignment, which is atomic; two fields that must change together are
not.

Two behavioural rules matter more than the code. A generator should be
**deterministic in time** — the frame at a given moment the same frame however
you arrived at it — because that is what makes an offline render reproducible;
the two shipped exceptions carry real simulation state and say so. And a
reactive generator must **fall back to its time-driven behaviour** at rest, so a
silent scene is still the generator you asked for.

## Where the Working Code Is

`docs/extending.md` carries the working examples, the display-mode and
interstitial-background surfaces, and the testing patterns that keep the suite
hardware-free. `CONTRIBUTING.md` has the development environment and the
conventions a change is expected to follow.
