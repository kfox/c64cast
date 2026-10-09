- **A video seek, jog, loop wrap or resume now lands on the frame at its
  target, with the sound from that moment.** It used to land on the
  keyframe before the target, up to one keyframe interval early, and show
  that picture labeled as the target; the sound started a little before the
  picture. A loop slot saved from a position read right after such a seek
  recalls the position it says, which can be a little later than the
  picture it was saved over. `start_s` (and a URL timestamp) now start on
  the exact moment the same way, and a file whose streams start after 0
  seeks to the right place.
