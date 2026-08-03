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

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`messages`**<br>`list`<br>*(required)* | List of message strings (or {text, color} tables) to scroll. |
| **`charset_path`**<br>`str \| None`<br>`None` | C64 character ROM used to rasterize the big glyphs (unset = the one c64cast dumped off your C64; see `--dump-char-rom`). |
| **`row`**<br>`str`<br>`'middle'` | Vertical placement: 'top', 'middle', or 'bottom'. |
| **`speed_cells_per_s`**<br>`float`<br>`8.0` | Scroll speed in character cells per second. |
| **`inter_message_pause_s`**<br>`float`<br>`1.5` | Pause between consecutive messages. |
| **`loop`**<br>`bool`<br>`True` | Loop the message list forever (false = play once then advance). |
| **`target_fps`**<br>`float \| None`<br>`None` | Override FPS used for px-per-frame snapping; unset = detect. |

## `callsign`

Static, unchanging text in a corner (callsign, booth ID, sponsor tag).

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`text`**<br>`str`<br>`''` | The fixed string to display. |
| **`corner`**<br>`str`<br>`'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>`str`<br>`'white'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Cell background color, or 'none' to leave the scene showing through. |

## `clock`

Current time (and optional date) in a screen corner.

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`corner`**<br>`str`<br>`'top-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`format`**<br>`str`<br>`'%H:%M'` | strftime format for the time line (e.g. '%H:%M'). |
| **`show_date`**<br>`bool`<br>`False` | Also show a second line with the date. |
| **`date_format`**<br>`str`<br>`'%Y-%m-%d'` | strftime format for the date line when show_date is true. |
| **`fg_color`**<br>`str`<br>`'white'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>`float`<br>`1.0` | Seconds between value recomputes (the text is repainted every frame). |

## `countdown`

Time remaining until a target date/time, in a corner.

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`target`**<br>`str`<br>*(required)* | Target datetime (ISO 8601, e.g. '2026-12-31T23:59'). |
| **`format`**<br>`str`<br>`'auto'` | 'auto' for adaptive units, or a template using {d}{h}{m}{s}. |
| **`done_text`**<br>`str`<br>`'DONE'` | Text shown once the target has passed. |
| **`corner`**<br>`str`<br>`'bottom-left'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>`str`<br>`'yellow'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>`float`<br>`1.0` | Seconds between value recomputes (the text is repainted every frame). |

## `logo`

Multi-line PETSCII art block loaded from a .txt file.

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`file`**<br>`str`<br>*(required)* | Path to a .txt file of PETSCII art (one screen row per line). |
| **`corner`**<br>`str \| None`<br>`None` | Corner to anchor the block (mutually exclusive with row/col). |
| **`row`**<br>`int \| None`<br>`None` | Explicit top row (use with col instead of corner). |
| **`col`**<br>`int \| None`<br>`None` | Explicit left column (use with row instead of corner). |
| **`fg_color`**<br>`str`<br>`'white'` | Art color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Background color, or 'none' to leave the scene showing through. |

## `marquee`

Single-line continuous ticker scrolling one text string with a separator.

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`text`**<br>`str`<br>`'C64CAST'` | The message to scroll continuously. |
| **`row`**<br>`int`<br>`0` | Screen row (0..24) the ticker scrolls along. |
| **`speed_cells_per_s`**<br>`float`<br>`3.0` | Scroll speed in character cells per second. |
| **`fg_color`**<br>`str`<br>`'yellow'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Background color (C64 color name). |

## `network`

Local IP / hostname / U64 ping latency in a corner.

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`items`**<br>`list \| None`<br>`None` | Which lines to show, any of: 'ip', 'hostname', 'ping'. |
| **`corner`**<br>`str`<br>`'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>`str`<br>`'light gray'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>`float`<br>`5.0` | Seconds between value recomputes (the text is repainted every frame). |

## `obs_status`

OBS Studio current scene + dropped-frame count (OBS WebSocket).

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`host`**<br>`str`<br>`'localhost'` | OBS WebSocket host. |
| **`port`**<br>`int`<br>`4455` | OBS WebSocket port. |
| **`password`**<br>`str`<br>`''` | OBS WebSocket password (if auth is enabled). |
| **`show_dropped`**<br>`bool`<br>`True` | Append the dropped-frame count to the status line. |
| **`corner`**<br>`str`<br>`'bottom-right'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>`str`<br>`'light green'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_s`**<br>`float`<br>`2.0` | Seconds between value recomputes (the text is repainted every frame). |

## `rss`

Ticker fed by a background RSS/Atom feed fetch.

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`url`**<br>`str`<br>*(required)* | RSS/Atom feed URL to fetch. |
| **`row`**<br>`int`<br>`0` | Screen row (0..24) the ticker scrolls along. |
| **`max_items`**<br>`int`<br>`10` | Maximum number of headlines to include in the ticker. |
| **`refresh_minutes`**<br>`float`<br>`15.0` | Minutes between background feed fetches. |
| **`speed_cells_per_s`**<br>`float`<br>`3.0` | Scroll speed in character cells per second. |
| **`separator`**<br>`str`<br>`'   *   '` | Text placed between consecutive headlines. |
| **`fg_color`**<br>`str`<br>`'light green'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Background color (C64 color name). |

## `scrolling_text`

One scrolling row of messages (per-row scroller).

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`messages`**<br>`list`<br>*(required)* | List of message strings to cycle through. |
| **`row`**<br>`int`<br>`24` | Screen row (0..24) to scroll along. |
| **`speed_cells_per_s`**<br>`float`<br>`6.0` | Scroll speed in character cells per second. |
| **`bg_color`**<br>`str`<br>`'black'` | Background color (C64 color name). |

## `spectrum_bitmap`

Audio spectrum as pixel-resolution bars painted into the mhires bitmap.

Restrictions: only on `mhires`.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`placement`**<br>`str`<br>`'bottom'` | Where the bars sit: 'bottom', 'center', or 'split'. |
| **`height_frac`**<br>`float`<br>`0.5` | Fraction of screen height a full-energy bar reaches. |
| **`gain`**<br>`float`<br>`1.0` | Multiplier applied to band magnitudes before bar height. |

## `spectrum_petscii`

Audio spectrum rendered as vertical color bars in screen RAM.

Restrictions: needs a PETSCII-compatible mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`placement`**<br>`str`<br>`'center'` | Where the bars sit: 'bottom', 'center', or 'split'. |
| **`height_rows`**<br>`int`<br>`12` | Height of the bar strip in character rows. |
| **`gain`**<br>`float`<br>`1.0` | Multiplier applied to band magnitudes before bar height. |

## `weather`

Temperature + conditions in a corner (background poll).

Restrictions: needs a text-capable mode.

<!-- table: fields -->
| Parameter | Description |
|---|---|
| **`provider`**<br>`str`<br>`'open-meteo'` | Weather source: 'open-meteo' or 'wttr.in'. |
| **`lat`**<br>`float \| None`<br>`None` | Latitude (open-meteo; with lon). |
| **`lon`**<br>`float \| None`<br>`None` | Longitude (open-meteo; with lat). |
| **`location`**<br>`str \| None`<br>`None` | Location name (wttr.in; alternative to lat/lon). |
| **`units`**<br>`str`<br>`'F'` | Temperature units: 'F' or 'C'. |
| **`corner`**<br>`str`<br>`'top-left'` | Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right). |
| **`fg_color`**<br>`str`<br>`'light blue'` | Text color (C64 color name). |
| **`bg_color`**<br>`str`<br>`'black'` | Cell background color, or 'none' to leave the scene showing through. |
| **`refresh_minutes`**<br>`float`<br>`10.0` | Minutes between background weather polls. |
