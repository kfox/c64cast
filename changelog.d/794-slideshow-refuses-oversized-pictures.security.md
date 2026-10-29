- **A slideshow skips a picture larger than 8192 × 8192 pixels instead of
  decoding it.** The size is read from the file's header first, so an
  uploaded PNG of a few megabytes can no longer declare a raster that decodes
  to gigabytes of memory on the render thread. A file whose contents are not
  PNG, JPEG, BMP or WebP is skipped with a warning too, whatever its
  extension.
