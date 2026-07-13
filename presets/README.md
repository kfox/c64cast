# WLED presets

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

These `.json` files are **taste/machine-specific captured data and are
gitignored** (only this README is tracked) — like `calibration/` and `assets/`.
