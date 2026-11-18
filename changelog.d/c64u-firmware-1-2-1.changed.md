- **The docs and `--describe` now say which features a C64 Ultimate on firmware
  1.2.1 supports.** A C64 Ultimate running 1.2.1 answered c64cast's check for
  each one: the live palette read (`host_palette = "auto"`),
  `hardware_palette = "source"`, `Vol Master`, the open-menu check, the
  `[midi_control]` `joystick` action and the idle close of the DMA connection. Each of these used to be
  documented as needing an Ultimate 64 on 3.15 or 3.15a. Bus Operation Mode's
  `Compatibility` value is also listed for the C64 Ultimate on 1.2.1. Nothing
  changes at runtime, because c64cast already detected these features by
  asking the machine.
