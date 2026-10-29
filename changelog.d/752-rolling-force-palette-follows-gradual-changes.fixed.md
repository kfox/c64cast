- **A live `force_palette` with `force_palette_colors = 16`, or with an
  explicit `force_palette_colors` list, now follows the picture as it
  changes.** On webcam, generative and `wled` sink scenes the forced palette
  kept the map it chose in the first second until a shot cut, because it only
  re-installed when the set of C64 colors changed, and with every color in use
  that set never does. It now re-installs once the newer map would repaint
  more than 15 % of the picture.
