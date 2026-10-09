- `register_web_routes`' `library`/`media` parameters defaulted to `None` and
  constructed real stores on demand, which resolve into the data directory
  and write there — a caller who forgot one got a component quietly writing
  under `~/.local/share/c64cast` instead of a `TypeError`. Both are now
  required, built once where `run_daemon` builds the config store.
