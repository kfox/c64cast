# assets/logos/

Plain-text PETSCII art for the `logo` overlay. Each file is a multi-line
text block that gets blitted into screen/color RAM at a corner or at an
explicit row/col.

## Example

```toml
[[scenes.overlays]]
type = "logo"
file = "assets/logos/ccug.txt"
corner = "bottom-left"
fg_color = "white"
```

## Format

- One row per line, max 40 chars wide (anything longer is clipped).
- Use printable ASCII for letters and digits; PETSCII screen-code bytes
  for graphics characters (`\xa0` solid block, `\xae` top-left arc, etc.).
- Trailing whitespace is treated as transparent.

The overlay uses the helper `overlays/__init__.py:ascii_to_screen()` to
translate ASCII bytes to screen codes, then writes the result via the
delta-cache so static logos generate ~0 traffic after the initial paint.
