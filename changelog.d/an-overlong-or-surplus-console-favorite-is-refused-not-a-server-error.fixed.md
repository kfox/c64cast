- **The web console refuses an overlong config ref or one favorite too many
  with a 4xx, not a server error.** Starring a 101st favorite, or a ref over
  512 bytes, answered 500. A start or switch naming a ref that long started the
  show and then answered 500; it is now refused with a 400 before anything
  starts, and the same command over the console's WebSocket is refused too. A
  ref that is not valid UTF-8 is refused with a 400 the same way.
