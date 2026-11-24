- **Quick playback shows the best colors the machine can.** `c64cast clip.mp4`
  now fits the Ultimate's own 16 colors to each video and picture, shot by
  shot, on an Ultimate 64 on firmware 3.15a or newer or a C64 Ultimate on
  1.2.1 or newer, and plays as before everywhere else. It uses the new
  `[color].hardware_palette = "auto"`, which does that without the warnings
  or refusals of `"source"`. A `[color]` table in your machine settings still
  decides.
