- **A camera hiccup no longer interrupts a vision gesture.** One frame the
  camera failed to deliver, or one the hand tracker couldn't read, restarted
  the 3-second pinch that resumes a paused show, and let a pose held through it
  pause or cycle a second time. Those frames are now skipped; a frame with no
  hand in it still ends the gesture.
