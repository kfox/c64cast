- **Quantized clips still launch when the external MIDI clock disappears.** A
  clock device unplugged, or a DAW that quit, without sending Stop used to
  freeze the beat grid, so a beat- or bar-quantized clip waited until clock
  came back. After two beats of silence the grid now carries on at the last
  measured tempo, and locks back on when clock pulses return.
