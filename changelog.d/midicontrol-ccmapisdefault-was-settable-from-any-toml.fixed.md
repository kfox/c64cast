- `[midi_control].cc_map_is_default` was settable from any TOML layer. It is
  derived run state — set False only when a layer really authored a `cc_map` —
  and writing it directly inverted the controller-profile merge with no
  `cc_map` in sight, because the `internal` metadata hides a field from
  `--describe`/the schema/the serializer but never gated the apply path. Fields
  marked internal are now treated like unknown keys.
