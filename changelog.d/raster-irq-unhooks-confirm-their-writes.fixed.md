- **The big-text overlay and the "UP NEXT" card no longer leave the C64
  hung or its keyboard dead after a write lost on the link.** Both unhook a
  raster IRQ by disabling the raster source, restoring `$0314` and re-arming
  CIA #1, and each of those writes could be dropped without an error: a lost
  disable behind a restored vector re-entered the IRQ on every frame, and a
  lost restore behind a re-armed CIA #1 sent the jiffy IRQ into RAM the next
  scene overwrites. Each write is now confirmed and retried, and anything left
  undone is logged. Both unhook through the same sequence as the hires and
  mhires teardown: CIA #1 and the raster source are masked first, `$0314` is
  restored whether or not those landed, a mask that was lost is written again
  behind the restore, and CIA #1 is re-armed only once the restore is
  confirmed, staying masked otherwise. The card now also re-arms CIA #1 after
  unhooking a leaked handler, so pause and skip keep working.
