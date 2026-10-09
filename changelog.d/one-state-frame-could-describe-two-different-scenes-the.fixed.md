- **One state frame could describe two different scenes.** The console snapshot
  re-read `playlist.current` four separate times, and a scene advance writes the
  index and the current scene as two separate statements with a teardown between
  them — so an interleaved advance emitted a frame naming scene A over scene B's
  effect rack and tune panel, with the layer indices the console then offered
  addressing a chain that had moved. The scene and index are sampled once.
