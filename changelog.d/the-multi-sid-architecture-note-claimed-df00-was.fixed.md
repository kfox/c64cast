- **The multi-SID architecture note claimed `$DF00` was accepted.** The decoder
  refuses any second/third SID address whose 25-byte register window reaches
  the REU command registers — zeroing those at teardown would point the audio
  ring's DMA at `$0000` — and the same document says so correctly a few
  hundred lines earlier. The multi-SID section now names the carve-out.
