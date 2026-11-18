- **A `launcher` scene that started a `.crt` no longer leaves the cartridge
  running.** The reset at the end of the scene, and the reset when c64cast exits,
  restarted the cartridge instead of returning the Ultimate to BASIC, so the
  game came back after the show ended. That reset now reboots the machine,
  which takes the cartridge out.
