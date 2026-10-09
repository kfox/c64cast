- **A crafted `.sid` header could put a SID on the REU's own registers.** The
  U64 firmware force-aligns a split UltiSID core's base *downward*, so the
  address the multi-SID planner emits is not the one the header declared — and
  the guard that refuses a declared base over hardware c64cast drives itself
  never saw the emitted one. A PSID v4 header whose second and third SID bytes
  are `$F2` and `$F6` declares `$DF20` and `$DF60`, both spec-legal; the `1/4`
  split that covers them realized at `$DF00`, and that live REST PUT put a SID
  on the REU status/command/address/length registers the DAC audio pump and the
  ASID ring player both drive — the same ones the audio NMI handler reads
  `$DF03` back from mid-transfer. Two bytes of a downloaded tune chose it. The
  planner now tests every `$20`-granular instance a split level would realize
  and falls through to the next level, refusing outright (caller warns and uses
  the canonical layout) when no level fits. The reserved ranges are one tuple
  shared with the header decoder instead of a second copy, and they now also
  carry the **Ultimate Audio sampler's `$DF20-$DFFF`** — the page the FPGA
  plays video audio out of, which the same two paths could reach and which
  scene teardown's 25-byte zero write would have walked mid-playback. A tune
  declaring an extra SID anywhere in `$DFxx` therefore degrades to single-SID,
  as one declaring `$DF00` already did; `$DE00-$DEE0` is unaffected.
