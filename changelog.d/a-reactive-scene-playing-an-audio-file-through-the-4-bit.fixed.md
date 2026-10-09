- **A reactive scene playing an audio file through the 4-bit `$D418` DAC no
  longer misses some onsets.** The DAC's playback clock moved in steps of one
  ring chunk, about 85 ms at 12 kHz, so the analyzer's window jumped by its own
  length and a click near the edge of the only window that held it never
  flashed. The same clicks missed on every run. The clock now moves
  continuously between chunks, which also smooths video played over the DAC
  without the REU pump.
