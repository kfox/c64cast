- **The web console's scrub bar no longer jumps to a wrong position for one
  poll around a seek or a resume.** The position was read from two pieces of
  state the seek updates one after the other. A seek or pause that failed
  half way through the audio cut also anchors on one reading of the sound
  card's clock now, instead of two taken a moment apart.
