- **A quiet ASID host no longer saturates the Ultimate DMA socket.** When the
  ring's write-ahead lead drains, the player pads "hold" slots so the SID keeps
  its last state. That padding had no pacing at all, so a spec-legal 16×
  multispeed stream that then went quiet padded at the link's maximum rate
  indefinitely — the entire measured write budget, spent on silence, with the
  render path queued behind it. Pads now go out in one batched write and cost a
  fixed handful of writes per second at any rate in the band.
