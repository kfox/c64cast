- `[ultimate64].sid_panning = 0` and `sid_volume = 0` passed validation and
  then silently auto-spread. Both validators opened with a truthiness guard
  meant for the empty list, which also swallowed a falsy *scalar* — and 0 is a
  meaningful value in both vocabularies (Center, and 0 dB), so a user asking
  for centered got `[-3, +3]`, the opposite. The scalar `-3` spelling of the
  same mistake was correctly rejected all along. `[hardware].host_sid_chips`
  had the same guard.
