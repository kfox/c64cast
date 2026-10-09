- **A remote video or audio URL whose server stops answering no longer
  freezes the show.** Opening a stream now gives up after 20 seconds, and a
  stream that goes silent mid-play gives up after 30 seconds without data, so
  the scene ends and the playlist moves on. Before, a server that accepted the
  connection and then sent nothing held the playlist on that scene forever.
