- The WLED sink (`wled_sink.py`, bridge Mode 2) rejected the wrong DDP flag as
  a "query" (`0x08` is STORAGE; QUERY is `0x02`), so a real discovery probe
  from LedFx/xLights/Jinx! slipped through as if it were pixel data and a
  STORAGE-flagged pixel packet was silently dropped. A TIME-flagged DDP packet
  (a 4-byte timecode ahead of the pixel payload) was also misparsed — the
  payload slice started 4 bytes early instead of accounting for the longer
  header. Both are now decoded per the DDP flag layout.
