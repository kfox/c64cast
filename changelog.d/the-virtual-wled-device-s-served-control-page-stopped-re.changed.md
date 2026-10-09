- The virtual WLED device's served `/` control page stopped re-rendering
  after touching almost any control — a slider drag, the color picker, the
  power switch, or saving a preset — because those all keep keyboard focus
  past the interaction, and `render()` skips its rebuild while any input is
  focused (so it won't yank a control mid-drag). Each now blurs once its own
  interaction actually ends, matching what the scene dropdown already did.
