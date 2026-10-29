- **An audio-file scene on a stream whose sound track sends nothing now
  stops.** The decoder only looked for the stop between packets of sound, so
  on a network stream carrying a picture but no sound it never saw one: the
  scene's teardown gave up waiting, and the decoder kept reading the stream in
  the background for as long as the server sent it.
