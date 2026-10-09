- **The DAC audio clock no longer jumps backward after the audio stalls.** On
  `$D418` DAC audio, when a video's decoder fell behind long enough to leave
  silence in the C64's sound ring and then caught up, the playback position
  stepped back by up to about a quarter of a second, then caught up again.
  A relative seek or an A/B loop mark taken in that moment landed short, and
  the on-screen timecode moved backward. The position now never moves
  backward during a scene.
