- **The scene snapshot names a slideshow's or launcher's files, not their full
  paths.** `SCENE_CONFIG_JSON`, written to be pasted into a public video
  description, carried a `slideshow` or `launcher` scene's `file` exactly as
  configured, so `/home/<account>/…` published the operator's account name and
  folder layout, and a URL entry kept any `user:password@` it carried. Each
  entry is now its last path component, and a URL is redacted, as video and
  SID scenes already were.
