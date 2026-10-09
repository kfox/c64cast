- **A `generative` scene with `audio_source = "sid"` and no `file =` failed on
  a normal HVSC tree.** The recursive walk of the default SID directory was
  keyed to the *error-message label* `"waveform"`, and this arm passes a
  different label while sharing the same default directory — so it got a shallow
  listing, and every documented HVSC layout has zero `.sid` files at the top
  level. It was refused with "the default directory 'assets/sids' is missing or
  empty" on the exact tree a waveform scene plays out of the box. The recursion
  is an explicit keyword now, which also means rewording an error message can no
  longer switch HVSC discovery off.
