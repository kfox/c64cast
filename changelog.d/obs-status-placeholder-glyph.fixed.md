- **`obs_status` reads `OBS ...` while it connects, not `OBS &`.** Its
  placeholder used a Unicode ellipsis, which the C64 screen-code conversion
  turned into an ampersand.
