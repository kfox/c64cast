- **SHIFT-cycling into a subtune that needs a full relaunch no longer leaves
  the oscilloscope on the previous song.** That path re-runs the player but
  never rebuilt the host emulator, so the scope drew song N-1's waveforms under
  song N's audio and could end the scene early watching the wrong song's
  envelopes decay. Every subtune now also gets the PLAY pre-flight that only
  the first-loaded song used to get — each subtune is its own entry point, so
  song 1 completing said nothing about song 2, and a spinning one was cued
  straight onto the real machine.
