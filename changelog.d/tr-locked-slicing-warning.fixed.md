- **The TeensyROM+ `dma_slicing = "locked"` warning no longer blames the
  firmware for a held bus.** When REU emulation or a pause holds the bus at
  connect, the warning now says so; when c64cast cannot tell a hold from
  firmware without the lock-to-store flag, it names both, and a probe that
  failed outright is reported with its error.
