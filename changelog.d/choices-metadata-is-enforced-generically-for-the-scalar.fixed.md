- `choices` metadata is enforced generically for the scalar config sections,
  rather than by a hand-written validator per field. The fields nobody had
  written one for failed *open*, and `[ultimate64].sid_video_mode` failed open
  into a machine retiming plus an HDMI output-mode switch (it is read as
  `!= "off"`), while `[hardware].host_sid_model`, `[teensyrom].storage` and
  `[ultimate64].hdmi_scan_resolution` silently did nothing. Two documented
  exemptions stay: `sid_play_rate` also takes a rate in Hz, and
  `[ultimate64].system` is matched case-insensitively because the hardware
  layer normalizes its case.
