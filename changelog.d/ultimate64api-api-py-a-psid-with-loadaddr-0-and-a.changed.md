- `Ultimate64API` (`api.py`): a PSID with `load_addr=0` and a `data_offset`
  leaving fewer than 2 payload bytes raised a bare `IndexError` instead of a
  clear `ValueError` while decoding the inline load-address header.
  `cue_song_reinit(song)` with `song` out of `1..num_songs` range (a bad
  caller index, or a stale UI control after switching tunes) reached
  `ParsedPsid.song_is_vsync`'s `speed >> bit` with a negative `bit` and
  raised `ValueError: negative shift count` instead of a message naming the
  actual problem; both now validate up front. `launch_program`,
  `run_sid_player`, and `dump_char_rom` flushed pending DMA writes and, on a
  failed flush, logged a warning but proceeded anyway into an irreversible
  `run_prg` reset that could race ahead of writes it depends on (the SID
  payload, the re-INIT stub, ...) — those three now abort with a
  `RuntimeError` instead. The three also shared one helper for the
  duplicated POST-then-404-to-`RuntimeError` pattern. `self.timeout`, set in
  `__init__` and never read (every call site takes its own `timeout`
  parameter), is removed. `urllib.parse.quote`, imported locally in two
  methods, is now a module-level import alongside the existing `urlparse`
  one.
