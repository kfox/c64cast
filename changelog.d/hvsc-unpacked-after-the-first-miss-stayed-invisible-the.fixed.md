- **HVSC unpacked after the first miss stayed invisible.** The songlengths
  lookups are process-global memos with no invalidation, including the "not
  found" answer — so in a long-lived `--serve` host, unpacking HVSC or fixing
  `[playlist].songlengths_file` could not take effect without restarting the
  process. The config-reload path clears them now. The "auto-detected HVSC
  database at …" line also moved behind the cache check, so it is logged once
  per process rather than once per waveform scene built.
