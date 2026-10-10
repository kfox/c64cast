- **Cancelling a performance-grid arm no longer freezes the bitmap clip on
  screen.** Re-arming a pad, or releasing a gate pad before its boundary,
  threw away the clip it had built by tearing it down, and that teardown
  reached the machine: a hires or mhires clip unhooked the bank-swap IRQ of
  the clip still playing, which kept staging frames that nothing flipped, and
  any clip could stop the audio the two share. A clip that never launched is
  now dropped without a teardown, and a bitmap mode unhooks only an IRQ its own
  setup installed.
