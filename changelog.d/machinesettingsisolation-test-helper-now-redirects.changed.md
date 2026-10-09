- `MachineSettingsIsolation` (test helper) now redirects `$C64CAST_DATA_DIR`
  alongside `$C64CAST_SETTINGS`, so a test module that opts into it cannot read
  or write the real `~/.local/share/c64cast/` either. Its name always read
  broader than it was.
