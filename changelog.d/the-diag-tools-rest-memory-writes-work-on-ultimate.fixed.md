- **The diag tools' REST memory writes work on Ultimate firmware 3.15.**
  `scripts/diags/_diaglib.py`'s `rest_writemem` sent the bytes in the URL of
  a POST, which firmware 3.15a refuses with HTTP 412 "Expected Body, but got
  none." — and the helper reported that only as a `False` nobody checked, so
  `run_and_capture.py --border-flash` drew no markers and said nothing. It now
  sends a PUT, the form 3.14, 3.15 and the C64 Ultimate's 1.1.0 all accept for
  up to 128 bytes, and a refused write raises with the firmware's error text.
  A failed border flash is printed.
