- **A Ctrl+C that lands while a write to a slow machine is stuck part way no
  longer garbles the commands after it.** The steps that still run after it
  used to go out on the same connection, where the machine read them as the
  rest of the cut write. On an Ultimate the connection is now dropped and
  reopened; on a TeensyROM+ the next command first waits about a second and a
  half for the cartridge to give up on the cut one.
