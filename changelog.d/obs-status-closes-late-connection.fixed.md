- **`obs_status` no longer leaves an OBS connection open after its scene
  ends.** A connection to a slow OBS that was still being made when the scene
  ended used to stay open afterwards; it is now closed as soon as it lands.
