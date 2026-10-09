- **The log redactor masks the values in a `Cookie` or `Set-Cookie` header.**
  `Cookie: session=abc123def` kept the session, because `session` is no secret
  name on its own. Every value in a `Cookie` header is masked. In a
  `Set-Cookie` header the attributes (`Path`, `Expires`, `HttpOnly`, …) stay
  readable and everything else is masked, including each cookie of a header
  that a client joined with commas. `--cookie` is read the same way.
