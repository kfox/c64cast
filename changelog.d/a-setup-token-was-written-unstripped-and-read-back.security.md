- A setup token was written unstripped and read back stripped, so a token
  pasted with a trailing space went out in the form's login link with the
  space and came back after the restart without it — the one link an
  appliance admin is given answering `401` forever, with no other way to
  learn the real token. Worse, 16 spaces passed the minimum-length check,
  stripped to `""` on read, and made the host mint a credential nobody had
  ever seen with the setup window already closed: recovery needed shell
  access or a reflash. Tokens are now stripped before every check, an
  interior newline is refused, and `MIN_TOKEN_LENGTH` moved to
  `control/auth.py` — enforced on the setup route as before, and now also
  warned about for a short `[web]`/`[control]` token from any source, since
  nothing here throttles login attempts.
