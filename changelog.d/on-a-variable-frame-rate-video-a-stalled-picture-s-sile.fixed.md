- **On a variable-frame-rate video, a stalled picture's silence fill now
  reaches as far past its frames as intended.** How many extra frames the
  buffer reads ahead was sized from the file's nominal frame rate; it now
  follows the frames' own timestamps.
