- **The log redactor masks the whole parameter list after `Digest`.**
  `Authorization: Digest username="u", response="abc…"` kept the `response`
  and the `cnonce`, because the spaces, commas and quotes in the list each
  ended the value.
