- **The Ultimate Audio sampler goes silent during a link outage instead of
  replaying audio it already played.** The sampler loops its REU ring on its
  own, so when the host's writes stopped it played the last lap again and
  again until the link came back, and the gate-off meant to stop it had to
  travel the same dead link. The channel's length register now marks the
  end of the audio the ring holds for the current lap, and the FPGA stops
  there by itself. Once the link is back the channel is restarted and the
  sound resumes.
