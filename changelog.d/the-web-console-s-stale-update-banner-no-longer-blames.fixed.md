- **The web console's stale-update banner no longer blames PyPI for a check
  nobody ran.** It read "No answer from PyPI since <date> — check its internet
  connection", but the staleness clock falls back to the last *attempt* when
  nothing has ever gone unanswered: on a laptop with no update timer, whose
  last check answered fine months ago, that named a service that was never
  asked and sent the operator after a network fault that did not exist. The
  banner now says what the login MOTD has always said — no check has
  *succeeded* in over N days — since that is the one thing true in both cases.
