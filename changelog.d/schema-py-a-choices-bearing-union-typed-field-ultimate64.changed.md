- `schema.py`: a `choices`-bearing union-typed field (`[ultimate64]
  sid_play_rate`, `str | float`) emitted a top-level `enum` alongside its
  `type: ["string", "number"]`, so the documented numeric form ("a number
  pins every vsync tune to that rate in Hz") failed schema validation in
  every editor pointed at the committed schema — `jsonschema.validate` on
  `50.0` raised `50.0 is not one of ['auto', 'off']`. Choices on a union
  now constrain only the string branch via `anyOf`, leaving the other
  branch(es) unconstrained. `_field_schema`'s `name` parameter, passed at
  every call site and never read in the body, is removed.
