- **A read-only viewer link no longer shows the host's absolute config paths.**
  The session status, the config root list and the session log handed a viewer
  the full path of the running config and of each config root, which names the
  operator's home directory. A viewer now sees only the config's name under its
  root, and the log and a failed start's error show the home directory as `~`.
