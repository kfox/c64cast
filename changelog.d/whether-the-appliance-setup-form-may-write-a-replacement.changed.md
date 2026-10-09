- Whether the appliance setup form may write a replacement admin token is now
  read off the credential resolution's own answer for *where* the running
  token came from, instead of being re-derived from the same environment and
  config reads a few lines away. The two expressions agreed, but a drift in
  either direction is a security failure — a form-written token silently
  outranked (locking the admin out at the next restart) or a deliberately
  configured one overwritten — and one of them was untested. The startup
  banner now also names which file or variable the running token came from,
  which is the only signal an operator gets that a pre-planted token file is
  being adopted.
