- **A slow U64 web server no longer makes `$D418` DAC audio replay itself.**
  The audio worker reads the C64's playback position over REST once per chunk,
  and a read slower than about 40 ms made it fall behind the player, which
  then replayed a lap-old ring. A slow read is now skipped, with a warning,
  and reading backs off until it is prompt again.
