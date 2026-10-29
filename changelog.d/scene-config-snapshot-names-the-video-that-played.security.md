- **The `SCENE_CONFIG_JSON` snapshot now names the video that actually
  played.** For a `file` spec with several entries it judged URL-or-local by
  the spec's first entry: a URL picked from behind a local entry was published
  as a "local file" with its query (and any access token in it) unredacted,
  and a local file picked from behind a URL was published as the whole spec.
  The source is now read off the picked entry, and a URL is redacted either
  way.
