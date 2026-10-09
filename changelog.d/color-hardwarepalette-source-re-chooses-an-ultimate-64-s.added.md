- **`[color].hardware_palette = "source"` re-chooses an Ultimate 64's own 16
  colors for each video and slideshow scene.** The palette is fitted to the
  scene's content (black, white and the three grays stay the machine's),
  pushed over the Command Interface before the scene paints, and the whole
  color pipeline aims at it; per-pixel color error over the bundled pictures
  falls to about a third. The machine's palette, a custom `.vpl` included, is
  put back before the next scene that does not use the setting and at exit, and
  pushed again after every reset c64cast issues. Needs Ultimate 64 firmware
  3.15a or newer; a C64 Ultimate on 1.1.0, older firmware and other machines skip it
  with a warning. Off by default, and refused alongside `force_palette` or
  `flicker_tolerance`.
