- **An ASID stream that grows to more SID chips no longer loses your mixer
  levels and pans at the end of the scene.** Each remap re-applies panning and
  volume, and the value it recorded as "original" was the one the previous pass
  had just written — so a core muted at setup and raised by the remap was put
  back OFF at teardown instead of at your own level.
