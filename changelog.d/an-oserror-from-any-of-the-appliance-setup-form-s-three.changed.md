- An `OSError` from any of the appliance setup form's three writes escaped as
  a bare `500` with no body, to an admin whose only interface to the box
  *is* that form. It now answers with the path that could not be written and
  the OS's own reason, and says that setup is still pending so a retry can
  recover. The token is also written *after* the connection now: it used to
  go first, so a failure writing `settings.toml` left the host's credential
  already replaced by one the `500` never handed back.
