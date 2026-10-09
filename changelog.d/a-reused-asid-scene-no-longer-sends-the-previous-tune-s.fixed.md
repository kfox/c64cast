- **A reused ASID scene no longer sends the previous tune's registers to the
  chip.** A flush writes the whole 25-byte register image, so the first frame of
  a new stream that touched a few registers carried the last tune's envelopes,
  pulse widths and filter settings along with it. Re-activation now clears the
  shadows with the rest of the stream state.
