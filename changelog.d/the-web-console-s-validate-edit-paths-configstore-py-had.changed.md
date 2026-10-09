- The web console's validate/edit paths (`config_store.py`) had a cluster of
  bugs stemming from the same design: `_capture_errors` attached its
  collector to the shared `c64cast` logger with no thread filter, so a
  `--serve` process's live-session workers (render/audio/DMA, on other
  threads) had their unrelated ERRORs folded into another request's report
  — and, via `_machine_layer_notes`'s unanchored `key not in blame`
  substring test, could misattribute a validation failure to a machine
  setting a short common key (`url`, `path`, `port`, `device`) merely
  happened to share with the failure text. The collector is now filtered to
  its own thread and capped at 200 records; blame now requires a
  word-boundary match on both the key and its section, checked only against
  `report["error"]` (not the captured log); and `_machine_layer_notes` now
  skips `SECRET_FIELDS` keys outright and never echoes a machine setting's
  `value` (only `path`/`section`/`key` — the attribution its own docstring
  argues for). `_validate_text_and_load`'s scratch-file `mkstemp` and the
  write that followed sat above the `try` whose `finally` unlinks it, so a
  write failure (ENOSPC, a remount to read-only) left `.c64cast-check-*.toml`
  behind — invisible to the listing — and escaped as a bare `OSError`
  instead of the `PathRejected` report the `mkstemp` half was already
  careful to produce; both now share one `try`/`except`. `validate_text`
  (and so `write`/`create`) had no size cap of its own — `write` enforced
  `MAX_BYTES` but the scratch file could still take an unbounded POST body
  onto disk first — now shared via one `_require_within_limit` every text
  entry point calls. Lastly, `_apply_edit` setattr'd an edit's raw JSON onto
  a container field (`overlays`, `[scenes.color]`, `hue_corrections`,
  `clips`) with no shape check, so a wrong-shaped value (a string for a
  list, a list of non-tables) reached `config_serialize`'s `[[...]]`
  emitters and raised a bare `TypeError`/`AttributeError`/`ValueError` —
  an unhandled 500 on an authenticated route — instead of `EditRejected`;
  `_apply_edit` now checks the value's shape against the field's own
  dataclass annotation before `setattr`, and `_rewrite`'s re-serialize call
  widens its `except` as a backstop. `describe()` also no longer shadows
  the module's `dataclasses.fields` import with a same-named local (latent
  today, but one field-list lookup away from `TypeError: 'list' object is
  not callable` from inside a request handler).
