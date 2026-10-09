- **The log redactor reads a name after a backslash escape as a name.**
  A logged bytes repr such as `b'Host: h\r\nCookie: sid=…'` kept the cookie,
  because the `n` of `\n` glued to `Cookie`; the same held for
  `Authorization:`, `Bearer`, `sig=` and a `--password` flag after `\n`, `\t`,
  `\xXX` or a JSON `\uXXXX` escape.
