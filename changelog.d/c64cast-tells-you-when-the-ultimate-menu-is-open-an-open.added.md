- **c64cast tells you when the Ultimate menu is open.** An open menu takes the
  keyboard and hides some or all of what c64cast draws, with nothing on the
  host side to say why. `--doctor` now reports it as a warning (`-v` logs the
  text the menu is showing), and a run warns once at startup if the menu is
  still open after its reset. The reset closes the menu in the default
  "Freeze" interface but not in "Overlay on HDMI". This needs Ultimate
  firmware **3.15** or newer. On older firmware, including C64 Ultimate
  1.1.0, the check is skipped with one log line saying so.
