- **`--init`'s advanced scene options no longer add an effect you didn't
  pick.** The `effect` prompt had no way to leave the field unset, so
  pressing Enter through it wrote `effect = "trails"` into the generated
  config. It now offers "(leave unset)" first.
