- **The big-text overlay and the "UP NEXT" card no longer leave the C64
  hung or its keyboard dead after a write lost on the link.** Both unhook a
  raster IRQ by disabling the raster source, restoring `$0314` and re-arming
  CIA #1, and each of those writes could be dropped without an error: a lost
  disable behind a restored vector re-entered the IRQ on every frame, and a
  lost restore behind a re-armed CIA #1 sent the jiffy IRQ into RAM the next
  scene overwrites. Each write is now confirmed and retried, the restore waits
  for the disable and the re-arm for the restore, and anything left undone is
  logged. The card now also re-arms CIA #1 after unhooking a leaked handler,
  so pause and skip keep working.
