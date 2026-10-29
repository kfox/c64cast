- **A page URL's password no longer reaches the `SCENE_CONFIG_JSON` snapshot.**
  A video `file` URL that yt-dlp resolves through its generic extractor came
  back as the snapshot's `webpage_url` exactly as given, so a
  `user:password@` link was written in the clear beside the redacted `url`
  field and rendered as "Original page" in the description the snapshot is
  meant for. `webpage_url` is now redacted the same way `url` is.
