- **A SID/display conflict could advise a change that did not clear it.** The
  overlap check reports the first conflicting region and looks at screen RAM
  before the bitmap, so a payload spanning both was reported as the screen
  conflict — and "load above $07E8" then lands straight in the hires bitmap.
  The remedy is worded off the highest region the display actually reserves.
