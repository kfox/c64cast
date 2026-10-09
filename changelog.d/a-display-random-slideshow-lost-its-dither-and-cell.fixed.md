- **A `display = "random"` slideshow lost its dither and cell strategy on the
  first slide.** The runtime re-pick rebuilt its display mode from a second,
  hand-written copy of the factory's wiring, and that copy had drifted: it
  passed neither `dither_method` nor `cell_strategy`, so the documented
  static-scene resolution (`[color].dither = "auto"` → floyd-steinberg,
  `cell_strategy = "auto"` → error-min) was replaced by "no dithering" and
  frequency allocation from the very first image onward. The same copy handed
  two facts to the flicker resolver and withheld them from the double-buffer
  resolver in the same breath, so a slideshow running the REU mic pump could
  end up installing the `$0314` raster IRQ the pump already owns. There is one
  wiring object and one entry point now, shared by the factory and the scene.
