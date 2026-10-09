- **A video's first sound now plays with its first picture.** The sound's clock
  used to start while the scene was still setting up, so the DAC played the
  sound at clip time 0 about 110-136 ms before its picture, and the sampler never
  showed the frames due in its first 0.2 s. The sound now waits for the first
  frame to be on screen, on the DAC, the sampler and the REU pump.
