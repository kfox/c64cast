---
number: C
generated: true
---

# Overlays

*Generated from the code by `scripts/gen_reference_appendices.py`.
Edits here are overwritten; run `make reference-appendices`.*

The 13 overlays and their 76 parameters. An overlay is attached to a scene with a `[[scenes.overlays]]` table; which ones a given display mode will accept is Appendix D.

## `big_text`

Demo-scene 8×-scaled horizontally-scrolling big text (blank/mcm only).

Restrictions: only on `blank`, `mcm`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `messages` | `list` | *(required)* | List of message strings (or {text, color} tables) to scroll. |
| `charset_path` | `str \| None` | `None` | C64 character ROM used to rasterize the big glyphs (unset = the one c64cast dumped off your C64; see `--dump-char-rom`). |
| `row` | `str` | `'middle'` | Vertical placement: 'top', 'middle', or 'bottom'. |
| `speed_cells_per_s` | `float` | `8.0` | Scroll speed in character cells per second. |
| `inter_message_pause_s` | `float` | `1.5` | Pause between consecutive messages. |
| `loop` | `bool` | `True` | Loop the message list forever (false = play once then advance). |
| `target_fps` | `float \| None` | `None` | Override FPS used for px-per-frame snapping; unset = detect. |

## `callsign`

Static, unchanging text in a corner (callsign, booth ID, sponsor tag).

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `text` | `str` | `''` | The fixed string to display. |
| `corner` | `str` | `'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| `fg_color` | `str` | `'white'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Cell background color, or 'none' to leave the scene showing through. |

## `clock`

Current time (and optional date) in a screen corner.

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `corner` | `str` | `'top-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| `format` | `str` | `'%H:%M'` | strftime format for the time line (e.g. '%H:%M'). |
| `show_date` | `bool` | `False` | Also show a second line with the date. |
| `date_format` | `str` | `'%Y-%m-%d'` | strftime format for the date line when show_date is true. |
| `fg_color` | `str` | `'white'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| `refresh_s` | `float` | `1.0` | Seconds between value recomputes (the text is repainted every frame). |

## `countdown`

Time remaining until a target date/time, in a corner.

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `target` | `str` | *(required)* | Target datetime (ISO 8601, e.g. '2026-12-31T23:59'). |
| `format` | `str` | `'auto'` | 'auto' for adaptive units, or a template using {d}{h}{m}{s}. |
| `done_text` | `str` | `'DONE'` | Text shown once the target has passed. |
| `corner` | `str` | `'bottom-left'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| `fg_color` | `str` | `'yellow'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| `refresh_s` | `float` | `1.0` | Seconds between value recomputes (the text is repainted every frame). |

## `logo`

Multi-line PETSCII art block loaded from a .txt file.

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `file` | `str` | *(required)* | Path to a .txt file of PETSCII art (one screen row per line). |
| `corner` | `str \| None` | `None` | Corner to anchor the block (mutually exclusive with row/col). |
| `row` | `int \| None` | `None` | Explicit top row (use with col instead of corner). |
| `col` | `int \| None` | `None` | Explicit left column (use with row instead of corner). |
| `fg_color` | `str` | `'white'` | Art color (C64 color name). |
| `bg_color` | `str` | `'black'` | Background color, or 'none' to leave the scene showing through. |

## `marquee`

Single-line continuous ticker scrolling one text string with a separator.

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `text` | `str` | `'C64CAST'` | The message to scroll continuously. |
| `row` | `int` | `0` | Screen row (0..24) the ticker scrolls along. |
| `speed_cells_per_s` | `float` | `3.0` | Scroll speed in character cells per second. |
| `fg_color` | `str` | `'yellow'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Background color (C64 color name). |

## `network`

Local IP / hostname / U64 ping latency in a corner.

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `items` | `list \| None` | `None` | Which lines to show, any of: 'ip', 'hostname', 'ping'. |
| `corner` | `str` | `'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| `fg_color` | `str` | `'light gray'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| `refresh_s` | `float` | `5.0` | Seconds between value recomputes (the text is repainted every frame). |

## `obs_status`

OBS Studio current scene + dropped-frame count (OBS WebSocket).

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `host` | `str` | `'localhost'` | OBS WebSocket host. |
| `port` | `int` | `4455` | OBS WebSocket port. |
| `password` | `str` | `''` | OBS WebSocket password (if auth is enabled). |
| `show_dropped` | `bool` | `True` | Append the dropped-frame count to the status line. |
| `corner` | `str` | `'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| `fg_color` | `str` | `'light green'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| `refresh_s` | `float` | `2.0` | Seconds between value recomputes (the text is repainted every frame). |

## `rss`

Ticker fed by a background RSS/Atom feed fetch.

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `url` | `str` | *(required)* | RSS/Atom feed URL to fetch. |
| `row` | `int` | `0` | Screen row (0..24) the ticker scrolls along. |
| `max_items` | `int` | `10` | Maximum number of headlines to include in the ticker. |
| `refresh_minutes` | `float` | `15.0` | Minutes between background feed fetches. |
| `speed_cells_per_s` | `float` | `3.0` | Scroll speed in character cells per second. |
| `separator` | `str` | `'   *   '` | Text placed between consecutive headlines. |
| `fg_color` | `str` | `'light green'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Background color (C64 color name). |

## `scrolling_text`

One scrolling row of messages (per-row scroller).

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `messages` | `list` | *(required)* | List of message strings to cycle through. |
| `row` | `int` | `24` | Screen row (0..24) to scroll along. |
| `speed_cells_per_s` | `float` | `6.0` | Scroll speed in character cells per second. |
| `bg_color` | `str` | `'black'` | Background color (C64 color name). |

## `spectrum_bitmap`

Audio spectrum as pixel-resolution bars painted into the mhires bitmap.

Restrictions: only on `mhires`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `placement` | `str` | `'bottom'` | Where the bars sit: 'bottom', 'center', or 'split'. |
| `height_frac` | `float` | `0.5` | Fraction of screen height a full-energy bar reaches. |
| `gain` | `float` | `1.0` | Multiplier applied to band magnitudes before bar height. |

## `spectrum_petscii`

Audio spectrum rendered as vertical color bars in screen RAM.

Restrictions: needs a PETSCII-compatible mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `placement` | `str` | `'center'` | Where the bars sit: 'bottom', 'center', or 'split'. |
| `height_rows` | `int` | `12` | Height of the bar strip in character rows. |
| `gain` | `float` | `1.0` | Multiplier applied to band magnitudes before bar height. |

## `weather`

Temperature + conditions in a corner (background poll).

Restrictions: needs a text-capable mode.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `provider` | `str` | `'open-meteo'` | Weather source: 'open-meteo' or 'wttr.in'. |
| `lat` | `float \| None` | `None` | Latitude (open-meteo; with lon). |
| `lon` | `float \| None` | `None` | Longitude (open-meteo; with lat). |
| `location` | `str \| None` | `None` | Location name (wttr.in; alternative to lat/lon). |
| `units` | `str` | `'F'` | Temperature units: 'F' or 'C'. |
| `corner` | `str` | `'top-left'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| `fg_color` | `str` | `'light blue'` | Text color (C64 color name). |
| `bg_color` | `str` | `'black'` | Cell background color, or 'none' to leave the scene showing through. |
| `refresh_minutes` | `float` | `10.0` | Minutes between background weather polls. |
