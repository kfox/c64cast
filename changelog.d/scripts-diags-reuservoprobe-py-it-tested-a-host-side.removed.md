- **`scripts/diags/reu_servo_probe.py`.** It tested a host-side servo on the
  REU pump's CIA #1 latch, a design that never shipped: the pump is held to
  the reader by the C64-side governor in its own IRQ handler instead. The
  probe had failed at import since the 8 kHz pump latch was renamed, and its
  latch writes went to CIA #2's Timer A, the NMI sample clock, instead of the
  pump's. `reu_margin_probe.py` measures the pump's lead over the reader.
