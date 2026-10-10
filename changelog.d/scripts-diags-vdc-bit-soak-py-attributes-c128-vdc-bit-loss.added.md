- **`scripts/diags/vdc_bit_soak.py` tracks lost VDC bits on a C128 back to the
  VRAM data line they were lost on**, through a TeensyROM+. It blits full
  80-column frames (or, with `--mixed`, five before/after patterns), reads video
  RAM back, and counts each wrong bit by data line and by what the bit held
  before. A fault confined to one line points at one VRAM chip, its socket, or
  the VDC; swapping chips, then the VDC, narrows it down.
