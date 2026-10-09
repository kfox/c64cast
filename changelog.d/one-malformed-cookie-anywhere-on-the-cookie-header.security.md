- One malformed cookie anywhere on the `Cookie` header discarded the whole
  jar, `c64cast_token` included — CPython's `SimpleCookie` bails on the
  first segment its pattern rejects and drops the morsels it already
  collected, *without raising*, so the `except Exception` that looked like
  the guard for this could never fire. Because browser cookies are scoped
  by host and ignore the port, any other service on the same box setting a
  cookie with an illegal character made the console permanently unreachable
  in that browser: a `401`, the login form, a fresh `Set-Cookie` that
  replaced ours and not the offender, and a `401` again — a login loop with
  nothing logged. The one morsel that matters is now parsed out of the
  header directly.
