- **A flood of ASID messages can no longer leave the SID sounding or outlive
  teardown.** The reader drained its MIDI port until the queue was momentarily
  empty, which under a backlog never happens — so the register flush that
  follows the drain was never reached (the chip held whatever was last written
  and kept playing) and the stop check that ends teardown was never re-read.
  The drain is now bounded per pass and re-checks the stop signal inside it.
