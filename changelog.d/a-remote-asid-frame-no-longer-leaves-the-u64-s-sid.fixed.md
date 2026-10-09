- **A remote ASID frame no longer leaves the U64's SID address map rewritten
  for good.** The scene snapshots the SID-address config before it remaps, and
  restores it on teardown — but the snapshot is deliberately first-call-wins,
  and setup's mixer pass folded its own values into the same record first, so
  the remap's snapshot silently captured nothing. Any peer on the MIDI/network
  port could then send one `0x50`-`0x5F` register frame and permanently change
  the machine's SID addressing (`Auto Address Mirroring` included), with only
  pan and volume put back. The baseline is now taken at setup, before anything
  folds. The regression test that was supposed to cover this only passed
  because its fixture skipped `setup()`; it now drives the real lifecycle.
