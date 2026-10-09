- **A warning when a tune's INIT does not finish running on the host
  emulator.** The scope and the reactive visuals are both drawn from a
  host-side 6502 running the same tune the SID chip plays, and a tune whose
  INIT is slow enough to hit the emulator's bound (a fat depacker, say) leaves
  that emulator holding only part of the register state the tune sets up. The
  tune still plays — the audio comes from the real chip — but the picture can
  disagree with it, and until now nothing said so unless you were running with
  `-v`. The line says which of the three things stopped the INIT, and notes
  that the detected PLAY rate may be affected too, since it is measured the
  same way. A tune picked from a pool is only reported on once it is the one
  being played, and a subtune is reported on once however many times you cue
  it.
