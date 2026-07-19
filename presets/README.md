# presets/ — legacy location

> **This is the *old* location.** As of the machine-settings / canonical-data-dir
> change, presets live under the **user data directory**, not the repo checkout:
>
> ```
> ~/.local/share/c64cast/presets/                 # Linux / macOS (XDG)
> %LOCALAPPDATA%\c64cast\presets\                  # Windows
> ```
>
> Override the whole data root with **`$C64CAST_DATA_DIR`**. `c64cast --doctor`
> prints the resolved location and, if it finds old files here, the exact `mv`
> to migrate them. The paths are resolved by
> [`c64cast/paths.py`](../c64cast/paths.py) (`presets_dir()` /
> `loop_presets_dir()`), so they work from a repo checkout, a `pip install`, or
> a PyPI wheel.

Two unrelated kinds of captured, taste/machine-specific data live under this
directory — both gitignored at this legacy location (only this README is
tracked), so an old file left here can never become committable.

## WLED presets

WLED "presets" captured through the virtual WLED device (bridge Mode 1) live
here, one JSON file per device name:

```
presets/wled-<sanitized-device-name>.json
```

Each file is the WLED presets map (`{"1": {...}, "2": {...}}`, ids 1–250; id 0
is WLED's reserved empty slot and is never stored). A preset snapshots the full
look of a moment — which scene is playing, its speed/intensity sliders, palette
mode, any forced colors, plus power and brightness — so it can be recalled in one
tap from the WLED app or c64cast's own `/` control page:

- **Save**: name the current state and store it (next free id).
- **Apply**: recall a preset. From the `/` page this restores perfectly even
  across a scene jump (the page replays slider/palette/color values once the
  target scene is live over WebSocket). From the third-party WLED app, recall is
  best-effort across a scene change (same-scene recall is exact).
- **Delete**: remove a preset.

Presets survive restarts (like real WLED, which persists them on the ESP32's
filesystem). See [`c64cast/wled_device.py`](../c64cast/wled_device.py)
(`PresetStore`) and the WLED section of [`CLAUDE.md`](../CLAUDE.md).

## MIDI live-tune loop presets

A performer's saved A/B loop points from the MIDI live-tune DJ transport's
Record/Stop + pad workflow (`[midi_control].cc_map`'s `loop_slot` action —
see [`CLAUDE.md`](../CLAUDE.md)'s "MIDI live-tune record workflow" note)
live under `presets/loops/`, one JSON file per video:

```
presets/loops/<slug>.<hash12>.json
```

The hash is derived from the video's basename+size (or the URL itself for a
URL-backed scene) — see `c64cast/transport.py`'s `loop_preset_key`/
`loop_preset_path` — so moving the file to a different directory keeps its
saved loops; editing its content (which changes its size) does not. Each
file holds up to a handful of numbered slots (pad numbers), each an A/B
point pair (`b: null` means "loop to end of file"). Safe to delete this
subdirectory at any time to reset every saved loop.
