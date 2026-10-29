- **The color pre-scan of a video it cannot seek now samples the whole clip.**
  For a clip of under twice the sample count it decoded every frame from the
  start and stopped halfway, so `auto_fit` and `force_palette` were fitted to
  the first half only.
