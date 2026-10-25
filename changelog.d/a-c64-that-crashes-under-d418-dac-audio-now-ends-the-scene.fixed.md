- **A C64 that crashes during a scene with $D418 DAC audio now says so and
  ends the scene.** Before, the only sign in the log was a warning that the
  NMI consumer had stalled, while c64cast went on streaming into a machine
  that was no longer running its program and the screen showed a text screen
  with no explanation. Now, when the audio stalls, c64cast looks once at the
  NMI vector and, on a hires or mhires scene, at the VIC's display mode. If
  either shows a crash it logs an error saying the C64 has crashed, ends the
  scene, and puts the machine back before the next scene starts. A run of
  one scene with `loop = false` stops there.
