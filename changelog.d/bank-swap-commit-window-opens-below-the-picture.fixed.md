- **Double-buffered `hires` and `mhires` video no longer shows the next frame
  in the bottom two pixel lines for a field.** The tear-free bank swap could
  commit from raster line 248, which is past the last cell row's badline but
  two lines short of the picture's end, so a swap taken there put the new
  bitmap under the bottom row's last two lines while the rows above still
  showed the old frame. The swap now waits for line 251, the first line below
  the picture on PAL and NTSC. The `big_text` overlay's raster interrupt moves
  to the same line, so its scroll no longer shifts those two lines early.
