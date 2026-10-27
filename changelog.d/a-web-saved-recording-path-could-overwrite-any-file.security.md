- **A config saved through the web console could overwrite any file the host
  user can write.** `[recording].path` was opened and truncated as written,
  so `path = "~/.ssh/authorized_keys"` in a show started from the console
  destroyed that file. A show started from the console (`--serve`) now keeps
  only the file name and records into the new `[web].recording_dir` (by
  default `recordings` under the data directory), which comes from the
  host's own launch config and never from the show. Every run also refuses a
  `path` that doesn't end in a video extension (`.mp4`, `.m4v`, `.mov`,
  `.mkv`, `.avi`, `.webm`), and never overwrites an existing file or follows
  a symlink: a name already taken records to `recording-2.mp4`, then
  `recording-3.mp4`, and so on.
