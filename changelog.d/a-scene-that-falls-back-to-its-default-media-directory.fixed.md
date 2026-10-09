- A scene that falls back to its default media directory now logs the absolute
  directory it resolved to. The defaults are relative, so they resolve against
  the process's working directory — which for a daemon, a systemd unit or a
  container entrypoint is not necessarily where the operator thinks the media
  is.
