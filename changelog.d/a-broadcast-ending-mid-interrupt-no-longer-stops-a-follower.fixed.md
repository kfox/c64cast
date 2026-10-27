- **A span broadcast that ended as a follower system picked it up could stop
  that system's playlist.** The conductor ends the broadcast before its scene
  finishes tearing down, and a follower handling the interrupt in that window
  raised an error out of its run loop. The follower now ignores a broadcast
  that has already ended, and one that ends while it is switching over just
  runs out at once.
