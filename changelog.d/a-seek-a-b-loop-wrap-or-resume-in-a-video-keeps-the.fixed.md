- **A seek, A/B loop wrap or resume in a video keeps the start of the sound
  it lands on.** The decoder can reach the new position and hand over its
  first sound before the old sound has been cleared out, and that sound was
  cleared out with it. The sound then started late, and after a seek into the
  last moment of a clip it did not play at all.
