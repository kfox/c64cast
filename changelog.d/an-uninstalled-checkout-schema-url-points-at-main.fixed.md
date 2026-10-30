- **A config written from a source checkout points its `#:schema` line at
  `main`.** Without package metadata the version reads `0+unknown`, which was
  written into the URL as a release tag that does not exist, so editors could
  not fetch the schema.
