- **A partly-torn-down system could be left holding hardware.** A failure part
  way through building one system's stack unwound by hand, in four different
  places, with no per-step guard — so a failing sampler restore stranded the
  API socket and the camera, and a failure anywhere after the REU provisioning
  step (an audio-streamer or preview construction failure) unwound nothing at
  all. Every resource is now registered on one ladder as it is acquired and
  released in reverse, each step guarded, whatever the failure.
