- `resolve_recording_path` measured "the user named this file" against the
  dataclass default rather than the machine-overlaid baseline every other
  layering decision uses — so a `settings.toml` carrying `[recording].path`
  made every system in an ensemble look explicit, skip the per-system stem,
  and point N `cv2.VideoWriter`s at one file. That is the collision the
  never-cascade entry exists to prevent, reached through the one layer that is
  supposed to count as unset, and only `--doctor` caught it.
