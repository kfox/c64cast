---
number: C
generated: true
---

# Overlays

The 13 overlays and their 76 parameters. An overlay is attached to a scene with a `[[scenes.overlays]]` table; which ones a given display mode will accept is Appendix D.

## `big_text`

Demo-scene 8×-scaled horizontally-scrolling big text (blank/mcm only).

Restrictions: only on `blank`, `mcm`.

```toml
  [[scenes.overlays]]
  type = "big_text"
  row = "middle"
  speed_cells_per_s = 8
  inter_message_pause_s = 1.5
  # also required: messages — has no default
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`messages`**<br>*Type:* `list`<br>*Default:* *(required)* | List of message strings (or {text, color} tables) to scroll. |
| **`charset_path`**<br>*Type:* `str \| None`<br>*Default:* `None` | C64 character ROM used to rasterize the big glyphs (unset = the one c64cast dumped off your C64; see `--dump-char-rom`). |
| **`row`**<br>*Type:* `str`<br>*Default:* `'middle'` | Vertical placement: 'top', 'middle', or 'bottom'. |
| **`speed_cells_per_s`**<br>*Type:* `float`<br>*Default:* `8.0` | Scroll speed in character cells per second. |
| **`inter_message_pause_s`**<br>*Type:* `float`<br>*Default:* `1.5` | Pause between consecutive messages. |
| **`loop`**<br>*Type:* `bool`<br>*Default:* `True` | Loop the message list forever (false = play once then advance). |
| **`target_fps`**<br>*Type:* `float \| None`<br>*Default:* `None` | Override FPS used for px-per-frame snapping; unset = detect. |

## `callsign`

Static, unchanging text in a corner (callsign, booth ID, sponsor tag).

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "callsign"
  corner = "bottom-right"
  fg_color = "white"
  bg_color = "black"
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`text`**<br>*Type:* `str`<br>*Default:* `''` | The fixed string to display. |
| **`corner`**<br>*Type:* `str`<br>*Default:* `'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'white'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Cell background color, or 'none' to leave the scene showing through. |

## `clock`

Current time (and optional date) in a screen corner.

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "clock"
  corner = "top-right"
  format = "%H:%M"
  show_date = false
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`corner`**<br>*Type:* `str`<br>*Default:* `'top-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`format`**<br>*Type:* `str`<br>*Default:* `'%H:%M'` | strftime format for the time line (e.g. '%H:%M'). |
| **`show_date`**<br>*Type:* `bool`<br>*Default:* `False` | Also show a second line with the date. |
| **`date_format`**<br>*Type:* `str`<br>*Default:* `'%Y-%m-%d'` | strftime format for the date line when show_date is true. |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'white'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>*Type:* `float`<br>*Default:* `1.0` | Seconds between value recomputes (the text is repainted every frame). |

## `countdown`

Time remaining until a target date/time, in a corner.

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "countdown"
  format = "auto"
  done_text = "DONE"
  corner = "bottom-left"
  # also required: target — has no default
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`target`**<br>*Type:* `str`<br>*Default:* *(required)* | Target datetime (ISO 8601, e.g. '2026-12-31T23:59'). |
| **`format`**<br>*Type:* `str`<br>*Default:* `'auto'` | 'auto' for adaptive units, or a template using {d}{h}{m}{s}. |
| **`done_text`**<br>*Type:* `str`<br>*Default:* `'DONE'` | Text shown once the target has passed. |
| **`corner`**<br>*Type:* `str`<br>*Default:* `'bottom-left'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'yellow'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>*Type:* `float`<br>*Default:* `1.0` | Seconds between value recomputes (the text is repainted every frame). |

## `logo`

Multi-line PETSCII art block loaded from a .txt file.

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "logo"
  fg_color = "white"
  bg_color = "black"
  # also required: file — has no default
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`file`**<br>*Type:* `str`<br>*Default:* *(required)* | Path to a .txt file of PETSCII art (one screen row per line). |
| **`corner`**<br>*Type:* `str \| None`<br>*Default:* `None` | Corner to anchor the block (mutually exclusive with row/col). |
| **`row`**<br>*Type:* `int \| None`<br>*Default:* `None` | Explicit top row (use with col instead of corner). |
| **`col`**<br>*Type:* `int \| None`<br>*Default:* `None` | Explicit left column (use with row instead of corner). |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'white'` | Art color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Background color, or 'none' to leave the scene showing through. |

## `marquee`

Single-line continuous ticker scrolling one text string with a separator.

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "marquee"
  text = "C64CAST"
  row = 0
  speed_cells_per_s = 3
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`text`**<br>*Type:* `str`<br>*Default:* `'C64CAST'` | The message to scroll continuously. |
| **`row`**<br>*Type:* `int`<br>*Default:* `0` | Screen row (0..24) the ticker scrolls along. |
| **`speed_cells_per_s`**<br>*Type:* `float`<br>*Default:* `3.0` | Scroll speed in character cells per second. |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'yellow'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Background color (C64 color name). |

## `network`

Local IP / hostname / U64 ping latency in a corner.

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "network"
  corner = "bottom-right"
  fg_color = "light gray"
  bg_color = "black"
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`items`**<br>*Type:* `list \| None`<br>*Default:* `None` | Which lines to show, any of: 'ip', 'hostname', 'ping'. |
| **`corner`**<br>*Type:* `str`<br>*Default:* `'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'light gray'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>*Type:* `float`<br>*Default:* `5.0` | Seconds between value recomputes (the text is repainted every frame). |

## `obs_status`

OBS Studio current scene + dropped-frame count (OBS WebSocket).

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "obs_status"
  host = "localhost"
  port = 4455
  show_dropped = true
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`host`**<br>*Type:* `str`<br>*Default:* `'localhost'` | OBS WebSocket host. |
| **`port`**<br>*Type:* `int`<br>*Default:* `4455` | OBS WebSocket port. |
| **`password`**<br>*Type:* `str`<br>*Default:* `''` | OBS WebSocket password (if auth is enabled). |
| **`show_dropped`**<br>*Type:* `bool`<br>*Default:* `True` | Append the dropped-frame count to the status line. |
| **`corner`**<br>*Type:* `str`<br>*Default:* `'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'light green'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>*Type:* `float`<br>*Default:* `2.0` | Seconds between value recomputes (the text is repainted every frame). |

## `rss`

Ticker fed by a background RSS/Atom feed fetch.

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "rss"
  row = 0
  max_items = 10
  refresh_minutes = 15
  # also required: url — has no default
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`url`**<br>*Type:* `str`<br>*Default:* *(required)* | RSS/Atom feed URL to fetch. |
| **`row`**<br>*Type:* `int`<br>*Default:* `0` | Screen row (0..24) the ticker scrolls along. |
| **`max_items`**<br>*Type:* `int`<br>*Default:* `10` | Maximum number of headlines to include in the ticker. |
| **`refresh_minutes`**<br>*Type:* `float`<br>*Default:* `15.0` | Minutes between background feed fetches. |
| **`speed_cells_per_s`**<br>*Type:* `float`<br>*Default:* `3.0` | Scroll speed in character cells per second. |
| **`separator`**<br>*Type:* `str`<br>*Default:* `'   *   '` | Text placed between consecutive headlines. |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'light green'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Background color (C64 color name). |

## `scrolling_text`

One scrolling row of messages (per-row scroller).

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "scrolling_text"
  row = 24
  speed_cells_per_s = 6
  bg_color = "black"
  # also required: messages — has no default
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`messages`**<br>*Type:* `list`<br>*Default:* *(required)* | List of message strings to cycle through. |
| **`row`**<br>*Type:* `int`<br>*Default:* `24` | Screen row (0..24) to scroll along. |
| **`speed_cells_per_s`**<br>*Type:* `float`<br>*Default:* `6.0` | Scroll speed in character cells per second. |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Background color (C64 color name). |

## `spectrum_bitmap`

Audio spectrum as pixel-resolution bars painted into the mhires bitmap.

Restrictions: only on `mhires`.

```toml
  [[scenes.overlays]]
  type = "spectrum_bitmap"
  placement = "bottom"
  height_frac = 0.5
  gain = 1
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`placement`**<br>*Type:* `str`<br>*Default:* `'bottom'` | Where the bars sit: 'bottom', 'center', or 'split'. |
| **`height_frac`**<br>*Type:* `float`<br>*Default:* `0.5` | Fraction of screen height a full-energy bar reaches. |
| **`gain`**<br>*Type:* `float`<br>*Default:* `1.0` | Multiplier applied to band magnitudes before bar height. |

## `spectrum_petscii`

Audio spectrum rendered as vertical color bars in screen RAM.

Restrictions: needs a PETSCII-compatible mode.

```toml
  [[scenes.overlays]]
  type = "spectrum_petscii"
  placement = "center"
  height_rows = 12
  gain = 1
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`placement`**<br>*Type:* `str`<br>*Default:* `'center'` | Where the bars sit: 'bottom', 'center', or 'split'. |
| **`height_rows`**<br>*Type:* `int`<br>*Default:* `12` | Height of the bar strip in character rows. |
| **`gain`**<br>*Type:* `float`<br>*Default:* `1.0` | Multiplier applied to band magnitudes before bar height. |

## `weather`

Temperature + conditions in a corner (background poll).

Restrictions: needs a text-capable mode.

```toml
  [[scenes.overlays]]
  type = "weather"
  provider = "open-meteo"
  units = "F"
  corner = "top-left"
```

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`provider`**<br>*Type:* `str`<br>*Default:* `'open-meteo'` | Weather source: 'open-meteo' or 'wttr.in'. |
| **`lat`**<br>*Type:* `float \| None`<br>*Default:* `None` | Latitude (open-meteo; with lon). |
| **`lon`**<br>*Type:* `float \| None`<br>*Default:* `None` | Longitude (open-meteo; with lat). |
| **`location`**<br>*Type:* `str \| None`<br>*Default:* `None` | Location name (wttr.in; alternative to lat/lon). |
| **`units`**<br>*Type:* `str`<br>*Default:* `'F'` | Temperature units: 'F' or 'C'. |
| **`corner`**<br>*Type:* `str`<br>*Default:* `'top-left'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>*Type:* `str`<br>*Default:* `'light blue'` | Text color (C64 color name). |
| **`bg_color`**<br>*Type:* `str`<br>*Default:* `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_minutes`**<br>*Type:* `float`<br>*Default:* `10.0` | Minutes between background weather polls. |
