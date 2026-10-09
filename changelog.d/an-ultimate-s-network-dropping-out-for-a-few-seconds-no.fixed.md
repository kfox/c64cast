- **An Ultimate's network dropping out for a few seconds no longer ends the
  scene.** A REU-staged video (`hires`, `mhires`, or a REU-staged character
  mode) whose link stayed down past one reconnect attempt stopped with
  `scene '…' raised; advancing`, and with `loop = false` the show ended. Now
  the frames are skipped, the log says the link is down (and again every
  10 s while it stays down), and the picture comes back when the link does.
  The sampler's audio still gives up after 10 s down, but its gate-off is now
  sent until it lands, so a long outage leaves the rest of the scene silent
  rather than looping the ring's last lap of audio (about 12 s at 44.1 kHz).
