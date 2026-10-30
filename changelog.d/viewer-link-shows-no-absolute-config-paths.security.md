- **A read-only viewer link no longer shows the host's absolute config or media paths.**
  The session status, the config root list, the media listing and the session
  log handed a viewer the full path of the running config, of each config and
  media root, and of each media file, which names the operator's home
  directory. A viewer now sees a config's name under its root and each media
  file's path under its root. The log and a failed start's error show the same
  names, and any other path under the home directory starts with `~`.
