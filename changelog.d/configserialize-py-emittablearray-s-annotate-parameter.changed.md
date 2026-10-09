- `config_serialize.py`: `_emit_table_array`'s `annotate` parameter was
  never read in its body, so `[[color.hue_corrections]]`/`[[scenes.overlays]]`/
  `[[scenes.color.hue_corrections]]` blocks got no per-param help comments
  even when the caller asked for them — the parameter is dropped rather
  than wired up, since nothing needed it. The four hardcoded
  `if sd.name == "color"`/`"performance"` branches inside `_emit_section`
  deciding which field renders as a `[[...]]` block are now one
  `_SECTION_TABLE_ARRAYS` lookup — which caught a real, independent gap in
  the same class while adding the drift test the fix calls for:
  `[midi_control] cc_map` (`list[dict[...]]`, and documented as
  `[[midi_control.cc_map]]` in its own help text) was falling through to
  `_fmt_value` and rendering as an inline array of inline tables; it now
  routes through the same block emitter. `_emit_scene` iterated only the
  fields `introspect` lists for a scene's current `type`, so a field the
  type doesn't claim but that carries a non-default value anyway (set
  while the scene was a different type, or by a structured edit) was
  silently dropped on every re-serialize — `load` never enforces
  `applies_to`, so this broke `load(dumps(cfg)) == cfg`, the module's own
  contract; such a field is now emitted alongside the type's own. A scene
  color override of exactly `{"hue_corrections": []}` serialized to a bare
  `[scenes.color]` header with nothing under it, reloading as `{}` — an
  empty override is still an authored key on the scene's sparse dict, so it
  now round-trips as an explicit `hue_corrections = []`. `SECRET_FIELDS`
  gained the `[web]`/`[control]` token pairs (see the config_store entry
  above) — it governs `dumps()`, `describe()`'s form and
  `_editable_fields()`, not `config_store.read()`'s raw text (documented on
  `SECRET_FIELDS` itself).
