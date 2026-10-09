- **A network dropout on an Ultimate that spans a scene change no longer
  spoils the next scene.** A scene that started while the link was down used
  to play as if its setup had reached the machine: a video's audio stayed
  silent for the whole clip, a SID scene was skipped, and other setup state
  (the display mode's IRQ, the sampler's gate) never arrived. Now the scene
  waits, the log says the link is down, and once it answers the scene is
  set up again and plays from the start. In an ensemble, the other systems
  can take the audio while it waits.
