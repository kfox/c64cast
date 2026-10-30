- **An appliance's setup form can be submitted only once.** After setup
  completed, the form kept answering until the host finished restarting, and a
  second unauthenticated submission in that gap could replace the token and
  connection target the admin had just chosen. A submission after the first
  now gets `409`, and `GET /api/setup` stops reporting setup as pending.
