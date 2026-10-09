- **A `viewer_token` was a full-control credential.** `GET
  /api/configs/{ref}` had no role check at all, and the auth gate's only
  viewer restriction is the HTTP method — so every `GET` passed for a
  read-only token, and that route returns the config file's *raw text*,
  including any `[web]`/`[control]` `token` and `[ultimate64].dma_password`
  it carries. Since `[web].config_roots` defaults to the directory the host
  was launched from, and `./c64cast.toml` is the documented home of
  `dma_password`, the file a guest could read was exactly the one holding
  the secrets: `GET /api/configs` for a name, `GET /api/configs/<ref>` for
  the admin token, and a link handed out to be read-only became remote
  control of the machine. Authorization now has a per-route seam
  (`auth.require_full`, with `SCOPE_ROLE_KEY`/`ROLE_FULL`/`ROLE_VIEWER` and
  `is_viewer` replacing six bare string literals across three modules — a
  misspelling in any of them evaluated False and *granted* write access),
  that route refuses a viewer with a `403`, and a contract test walks the
  assembled app and fails on any viewer-reachable route that nobody has
  classified, which is the part that stops the next one. Browsing config
  *names*, the media listing, the screen and the state feed stay
  viewer-readable — that is what a read-only link is for.
