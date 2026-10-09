- **An `[audio_features].fft_size` above 32768 is refused when the config
  loads.** Any size passed, and a mistyped one such as `17179869184` made the
  first reactive scene try to allocate hundreds of gigabytes.
