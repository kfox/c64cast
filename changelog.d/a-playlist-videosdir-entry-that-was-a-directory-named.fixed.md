- **A `[playlist].videos_dir` entry that was a directory named `clips.mp4`
  became an interleaved video.** The interleave lister filtered on the extension
  alone with no regular-file check, unlike the identical listing a scene's
  `file =` gets, so the entry only failed once PyAV tried to open it. Both go
  through one lister now.
