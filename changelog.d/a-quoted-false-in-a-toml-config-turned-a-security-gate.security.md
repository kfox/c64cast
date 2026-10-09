- **A quoted `"false"` in a TOML config turned a security gate on.**
  `[control] allow_unauthenticated = "false"` and `[web] setup_wizard =
  "false"` stored the *string* `"false"`, and every consumer of a bool field
  is a plain truthiness test — so both read as **on**, which for
  `allow_unauthenticated` short-circuits the refusal that stops an
  unauthenticated control plane binding to the LAN, and for `setup_wizard`
  serves the one-time *unauthenticated* setup form (whoever reaches it first
  picks the connection target and the console token) instead of the
  token-gated app. A field annotated exactly `bool` now refuses a non-bool
  value, naming the section and key; the tri-states (`bool | str`, e.g.
  `[video].use_reu_staged`) are untouched.
