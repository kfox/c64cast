- **mhires keeps gray things gray.** A light gray wall or a gray test bar no
  longer renders as cyan, and a black-and-white film no longer picks up cyan
  and brown speckle. The gray penalty that keeps pale colors colorful now
  weighs on a pixel in proportion to how colorful it is, and a neutral pixel
  prefers a gray. That holds with `flicker_tolerance` off, the default; with
  blends on, only the gray penalty is scaled. The new `[color].neutral_keep`
  setting, on by default, turns it off.
