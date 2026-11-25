- **A video's hardware palette now follows its shots.** With
  `[color].hardware_palette = "source"`, each shot gets its own 16 colors,
  fitted to up to its first two seconds and changed on its first frame, and a
  dissolve that drifts away from them gets a new set. Faces and warm scenes no
  longer bend toward one palette for the whole video. Set
  `[color].hardware_palette_shots = false` for one palette per video.
