- The `/perf` page is served with `Content-Security-Policy`
  (`frame-ancestors 'none'`), `X-Frame-Options: DENY` and
  `X-Content-Type-Options: nosniff`. Hardening rather than a fix: it is a fixed,
  server-authored body with no caller content in it, and the clickjacking the
  headers refuse is strictly harder than what the `Origin` check above closes.
