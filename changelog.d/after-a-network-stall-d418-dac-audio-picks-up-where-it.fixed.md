- **After a network stall, `$D418` DAC audio picks up where it should instead
  of overwriting itself.** If the link to the C64 stalled for more than about
  a third of a second, the audio worker came back far behind its schedule and
  wrote as fast as the link allowed to catch up. That overran the audio it had
  just written before the C64 could play it, garbling up to as long as the
  stall itself, and squeezed the video's writes. The worker now restarts a
  safe distance ahead of the C64's playback and logs a warning. Live mic input
  that piled up during the stall is dropped rather than played late, and a
  warning says how many seconds of it went, whether or not the worker had to
  restart.
