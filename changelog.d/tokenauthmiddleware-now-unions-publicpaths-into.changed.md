- `TokenAuthMiddleware` now unions `PUBLIC_PATHS` into `public_paths` itself
  rather than trusting each caller to, which is what `install_auth`'s
  docstring already promised; the introspection cache is built under a lock,
  so the "built once" comment above it is true even when two cold requests
  arrive together; and an empty `Authorization: Bearer` header (what some
  proxies emit for an unset credential) falls through to the next token
  source instead of suppressing a valid cookie and answering `401`.
