- **A populated directory whose name contains `[`, `*` or `?` was reported as a
  glob that matched nothing.** An existing *file* already won over glob
  interpretation — `Clip [videoid].mp4` is yt-dlp's own naming convention — but
  a directory did not, and the same convention produces such directories for
  playlist downloads. `os.path.isdir` is now tested before the glob branch, like
  `os.path.isfile` already was.
