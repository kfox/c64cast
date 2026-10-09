- The web console's config store (`config_store.py`) treated `[web].token`/
  `token_file`/`viewer_token` and `[control].token`/`viewer_token` as
  ordinary fields: `config_serialize.SECRET_FIELDS` only ever named the DMA
  password, so `describe()`'s form data and `_editable_fields()` handed a
  viewer-role `GET /api/configs/{ref}` the console's own admin token —
  turning a shared "watch the show" link into full control of the host.
  `SECRET_FIELDS` now also names the five token fields, which (matching the
  DMA password) makes `describe()`/`patch()` withhold them and makes a form
  save refuse a file that carries one rather than silently drop it on
  re-serialize. `read()`'s raw `text` still carries any secret verbatim —
  gating that behind the full-token role, or masking a secret assignment in
  it, needs a role in hand and belongs to `web_api`/`auth`, not this store;
  documented on `read()` rather than guessed at here. Alongside it: the
  ref/write jail (`resolve()`) enforced `.toml`-suffix and root-containment
  but let a ref name a file `NON_CONFIG_NAMES`/`NON_CONFIG_DIRS`/the dotfile
  rule already hides from the listing — a read/write primitive for
  `.cargo/config.toml`, `pyproject.toml` and the like on the cwd-fallback
  root; those rules now gate `resolve()` itself, not just `_walk`.
  `_require_writable` decided read-only by the ref's *label* rather than by
  path containment, so a source checkout's cwd root (which physically
  contains the packaged examples underneath it) could reach and overwrite
  them through its own writable label; it now checks containment against
  every root. `_validate_text_and_load` and `read()` handed submitted text
  (or a file already on disk) straight to `config.load_master`, which opens
  an `[ensemble].systems[].config` path verbatim when absolute — a read
  primitive for any file on the host, since a parse failure on the named
  target embeds its path, a source line and a caret; both now refuse before
  the text ever reaches the loader, with a fixed, non-echoing message.
