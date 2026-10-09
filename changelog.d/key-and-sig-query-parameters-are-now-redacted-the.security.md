- **`?key=` and `?sig=` query parameters are now redacted.** The pattern
  required the literal `api` before `key` and did not know `sig` at all, so the
  two spellings signed media and feed URLs use passed through to `--log-file`
  and the console's log buffer — and `-vv` widened that reach, since urllib3's
  per-request record carries the query string of a user-supplied RSS or HTTP
  video URL. `signature` and a `_`- or `-`-separated prefix (`signing_key`,
  `X-Amz-Signature`) are covered too. A glued prefix is not, so `sortkey=`,
  `hotkey=` and `sig_level=` keep their values and stay diagnostic.
