- `[color].hue_corrections` concatenated across layers instead of overriding.
  Machine settings declaring band X plus a project TOML declaring band Y gave
  `[X, Y]`, with no way for the project file to replace, reorder or remove X —
  against the documented precedence, and against `scene_color`, which has
  always treated the same field as an all-or-nothing replace. It also made the
  `load(dumps(cfg)) == cfg` round trip lossy (a list-of-tables is written whole
  or not at all, so `[X, Y]` reloaded as `[X, X, Y]`). A layer that declares
  the key now replaces the list; one that stays silent inherits it.
  `hue_corrections_replace_defaults` keeps its own separate meaning against the
  built-in purple rescue.
