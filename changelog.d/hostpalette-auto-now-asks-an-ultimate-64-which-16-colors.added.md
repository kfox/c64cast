- **`host_palette = "auto"` now asks an Ultimate 64 which 16 colors it is actually
  driving**, instead of assuming the built-in table. Every color decision in
  the pipeline is a distance measured against that table, so a machine running
  a custom `.vpl` palette was being quantized against the wrong 16 — a wrong
  table does not tint the picture, it changes which color each pixel becomes.
  The read goes over the Ultimate Command Interface, costs one round of
  register polling at startup, and uploads no 6502 code. It needs Ultimate 64
  firmware **3.15a** or newer, which added runtime palette control to the Command
  Interface. Anything that cannot answer — older firmware, a failed read —
  falls back to the built-in table exactly as before, including the existing
  warning that names the loaded `.vpl`.
