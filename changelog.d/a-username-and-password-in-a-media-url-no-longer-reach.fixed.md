- **A username and password in a media URL no longer reach `--log-file`,
  the web console's log, or its config-check report.** When a private
  `https://user:token@…` audio or video file failed to open, the error
  quoted the whole URL, and only `token=`/`sig=`-style values were masked.
  The `user:token@` part is masked now as well, and so is the `hmac=`
  signature in an Akamai `__token__=` or `hdnts=` parameter, URL-encoded
  (`hmac%3D…`, `%26sig%3D…`) or not. So are `pwd=`, `passwd=`,
  `passphrase=`, `passcode=`, `loginpas=`, `pass=`, `auth=`, `jwt=` and
  `credential(s)=` values, in a query string, a `key=value` or `key: value`
  pair, or JSON: an IP camera's `videostream.cgi?user=admin&pwd=…` or
  `?loginuse=admin&loginpas=…` URL kept its password before. A key that
  runs on past one of those names, such as `author=` or `pass_count=`, keeps
  its value, and so does one with `pass` or `auth` glued onto its end, such
  as `bypass=` or `oauth=`. The terminal
  still shows the URL as it was.
