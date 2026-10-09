- **The log redactor now reads a line instead of pattern-matching it, which
  closes a dozen shapes that let part or all of a secret through to
  `--log-file`, the web console's log tail and the scene snapshot.** Among
  them: a `'` inside a password (`token='it's@er2'`), any `Authorization:`
  scheme other than `Bearer` (`Basic …`, `token …`), a quoted or
  comma-led Bearer credential, `Bearer%20Bearer%20…`, an encoded tab or
  newline after `Bearer`, a `+` or an encoded space inside a credential, a
  name inside another one's quoted value, and a `token: b'…'` bytes repr.
  Each line is now percent-decoded to a fixed point and read for keys, auth
  schemes and URL userinfo wherever they start, and an encoded value ends at
  the delimiters of its own level of encoding. Two naming fixes ride along:
  glued names such as `userpass=` and `authkey=` are now masked, and
  `high-pass=` and the shell's `PWD=` keep their values. A `bearer` key, a
  `=>` separator and an `Authorization:` value whose first word is no known
  scheme are masked as well, as is a `sig=` or `key=` after a JSON-escaped
  `&` in a URL quoted inside a JSON string, and the word after a flag such
  as `--password` or `--video-password` in a logged command line.
