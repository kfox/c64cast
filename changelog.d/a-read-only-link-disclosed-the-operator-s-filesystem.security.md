- **A read-only link disclosed the operator's filesystem layout.** The state
  frame carried `tuned.config_path`, the absolute path of the running show file,
  and both `GET /perf/state` and the socket pushes are read methods — so a
  viewer token learned the operator's username and directory layout, which is
  reconnaissance for the config-store routes the same host exposes. A viewer now
  gets an empty `config_path`; `config_name` (already on the wire) is all the
  page used it for.
