- Config field metadata corrections, all of which render into `--describe`, the
  committed JSON Schema, the annotated example TOML and the web console:
  `applies_to` now means scene types and nothing else (three `[color]` flicker
  fields were passing *display-mode* names and two `[ultimate64]` fields a
  *backend* name through the same key, which the first generic consumer would
  have read as "matches no scene type"; those five say it in their help
  instead, and a test pins the vocabulary); `[[scenes]].overlays` declares the
  types that accept one, so `--describe scene:launcher`, the wizard and the
  console stop offering a key the loader hard-rejects;
  `[midi_control].cc_map`'s help builds its `action` list from the constant
  the loader validates against, having fallen four actions behind it
  (`tempo_tap`, `clip_launch`, `fx_toggle`, `osd.position`);
  `[audio].sampler_sample_rate`'s help pointed at a 6.25 MHz reference clock
  the code no longer divides by, contradicting its own sibling field and the
  shipped default; `[[performance.clips]]`' help states the five defaults that
  previously existed only inside the validator (`quantize` defaults to the
  bar, not to the `"off"` its help listed first); `[web].viewer_token`'s help
  says what the read-only tier can actually see; three more C64-color fields
  declare the `c64color` vocabulary so the console offers swatches instead of
  a blind text box; and two comments pointing at `config.resolve_*` resolvers
  that live in `scene_factory` are requalified.
