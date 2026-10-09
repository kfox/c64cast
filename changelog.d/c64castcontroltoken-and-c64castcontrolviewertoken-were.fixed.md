- `C64CAST_CONTROL_TOKEN` and `C64CAST_CONTROL_VIEWER_TOKEN` were dead in
  ensemble mode. The plane that binds reads the master's `[control]`, an
  object no `merge_cli` call ever touches, so the env fold landed on N
  per-system configs nothing reads while the plane came up on whatever token
  the shared master file declared — the opposite of what the field's own help
  promises. An operator who rotated the real token into the environment was
  running on the placeholder anyone with repo access had already read.
