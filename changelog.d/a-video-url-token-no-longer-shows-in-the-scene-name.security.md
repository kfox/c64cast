- **A video URL's access token no longer shows in the scene name.** A `file`
  URL with no `name` took its scene name from the last path segment with its
  query still attached, so a signed link's token was drawn on the "UP NEXT"
  card and written into the `SCENE_CONFIG_JSON` snapshot meant for a public
  video description. The name now comes from the URL's path alone.
