- **An overlay that fails mid-scene is still torn down, and comes back on the
  next lap.** When an overlay's setup or drawing raised, it was switched off
  for the scene and its teardown was skipped: the big-text overlay left its
  raster IRQ hooked and the keyboard masked into the next scene, and the
  RSS, weather, network and OBS status overlays left their poll threads
  running. It also stayed off for the rest of a looping playlist. Each
  overlay is now torn down whether or not it was switched off, and switched
  back on at its scene's next setup.
