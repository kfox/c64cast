- **A scene that fails to start is skipped instead of stopping the show.** A
  scene whose setup raised an error (a MIDI scene whose keyboard is unplugged,
  a program the launcher could not send) used to end the whole run with
  "playlist advance failed; aborting". It is now logged, torn down and skipped,
  and the playlist moves on to the next scene. A playlist with only that one
  scene still stops with an error and exit status 1, and so does a playlist
  none of whose scenes can start.
