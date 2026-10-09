- The web console's media browser (`media_store.py`) listed a directory as a
  browsable entry straight off its unfiltered file list, before the per-file
  symlink-escape check below it ever ran — so a directory whose only
  kind-matching member was a symlink pointing outside its root
  (`ln -s /home/other/private.mp4 assets/videos/leak.mp4`) was still offered
  as a listed entry, and `resolve_file_spec` treats a listed directory as a
  randomizer that picks a member at each scene `setup()`, following that
  symlink onto HDMI — reachable by anyone with local or group write access to
  a media root, not the HTTP surface (uploads only ever create regular
  files). `_candidates` now filters a directory's hits against the jail check
  before deciding whether to yield the containing directory at all, so the
  directory and file listings agree about what's actually inside the root.
