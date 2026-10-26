- **`force_palette` on a long video now picks its colors from the whole
  video.** The pre-scan stopped collecting pixels once its sample budget
  filled, partway through the sampled frames, so a palette
  for a video whose later scenes changed color was chosen from its opening
  alone. Every sampled frame now counts equally.
