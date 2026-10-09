- `auth._safe_next` rejected `//host` but not `/\host`, which a browser also
  resolves offsite; what kept the login redirect on-site was Starlette's
  percent-encoding rather than the validator's own check.
