- **An `osd.position` pad's double-tap hide now lasts past the current scene.**
  It wrote the per-scene static gate, which nothing re-stamps, so a pad hit to
  clear the audience screen quietly un-hid itself on the next auto-advance —
  and since the web console's PERF button hides the same OSD and *does*
  persist, the two controls hid one thing to two different depths. The pad now
  turns performance mode on and off, so it reaches as far as PERF does and a
  tap still brings the OSD back whichever gate is holding it down, including
  the config's own `[midi_control].osd = "off"`.
