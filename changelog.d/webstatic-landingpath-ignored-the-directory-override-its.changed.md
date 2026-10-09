- `web_static.landing_path` ignored the `directory` override its four
  siblings honor, so a host serving the console from a non-packaged bundle
  computed `/perf` for the startup URL, the read-only link and the setup
  form's login link. Latent (production never passes one), but it made
  `landing_path` the one function there whose answer could not agree with
  what was mounted.
