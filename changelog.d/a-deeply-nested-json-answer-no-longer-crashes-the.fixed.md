- **A deeply nested JSON answer no longer crashes the connect.** Anything
  answering at the Ultimate's address with a JSON body nested some 60,000
  levels deep (60 KB of `[`) made Python's decoder run out of stack, and the
  error that raised slipped past the handlers meant for a bad answer: the
  device-identity line and the config-category probe at connect, and the
  keyboard and joystick input reads, raised into the run instead of treating
  the answer as unreadable. They now handle it like
  any other body that isn't JSON. The diag tools' REST config read and write
  in `scripts/diags/` do too, and also no longer raise on a body that is JSON
  but not an object.
