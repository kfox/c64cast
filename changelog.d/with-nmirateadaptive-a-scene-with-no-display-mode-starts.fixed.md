- **With `nmi_rate_adaptive`, a scene with no display mode starts at the
  nominal rate.** The previous scene's display mode outlived its stop, so such
  a scene after an `mhires` one began at `mhires`'s faster learned rate (sharp
  until the loop walked it back) and then filed its own settled rate under
  `mhires`, mis-seeding the next `mhires` scene.
