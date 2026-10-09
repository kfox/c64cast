- `char_rom.py`'s load path (`_read_glyphs`, behind every `load_glyphs`
  call) only length-checked a resolved charset, while `install_data` ran the
  full structural `verify()` — so a stale hand-copied file, a wrong file at
  a configured `charset_path`, or any other 2 KB file at a resolved path
  rendered garbage glyphs with no diagnostic, and (since `ensure_installed`
  treated any non-`None` `resolve()` as "already have one") permanently
  suppressed the auto-dump that would have fixed it. The load path now runs
  the same `verify()`, falling back to the builtin font with a warning
  naming the reason; `ensure_installed`'s gate now requires that resolved
  file to actually verify before it counts as "nothing to do". A configured
  `charset_path` that doesn't exist at all was also silently absorbed by
  `resolve()`'s fall-through with no record anywhere despite the module's
  own docstring promising "a warning from the caller" — `_read_glyphs` now
  logs one, naming the configured path and what it fell back to.
  `video.framebuffer.Framebuffer`'s own duplicate pre-check for exactly this
  case is removed now that every caller gets the same diagnostic centrally.
  Also fixed: an inverted verification-rationale docstring (an all-`$00`/
  all-`$FF` buffer *fails* the reverse-video complement check outright; the
  `$20`-blank/`$01`-not-blank pair is what catches a buffer whose halves
  complement *by construction*, which the complement check cannot see
  anything wrong with) and a British "synthesises" in the module docstring.
  New/updated cases in `tests/test_char_rom.py` and `tests/test_framebuffer.py`.
