- `MediaStore.receive`'s upload commit (`media_store.py`) had three related
  gaps. A failed `flush()`/`fsync()` (disk full) left the abort handler's own
  `file.close()` re-raising the same `OSError` a second time before
  `os.unlink` ever ran, orphaning the up-to-512-MB `.part` file the module's
  own docstring promises never survives a failure. Separately, `_unique_name`'s
  `-2`/`-3` collision suffix could lengthen an already-at-the-cap name past
  the filesystem's own limit, and an embedded NUL byte passed every
  structural check (`Path.exists()` silently swallows the `ValueError` a NUL
  raises) — both then died inside `os.replace` as an untyped
  `OSError`/`ValueError` that no caller's `MediaStoreError` mapping could
  classify, turning a name the store meant to refuse into an unhandled 500
  after the whole body had already been streamed. And `destination()`'s
  docstring promised a jail re-check against the joined path that no caller
  actually ran; on Windows — a first-class target per `paths.py`'s
  `os.name == "nt"` branches — that's exploitable outright, since
  `PureWindowsPath('D:/media') / 'C:evil.prg'` discards the left operand
  entirely, landing a drive-relative name wherever the process happened to
  be on that drive. `receive` now suppresses `OSError` from its own cleanup
  so it can never replace the failure that triggered it, and re-checks
  `directory / final_name` against the root before `os.replace`;
  `_unique_name` now rejects a `-2`/`-3` candidate that would cross
  `_MAX_NAME_BYTES` itself (`MediaNameRejected`, before `os.replace` ever
  sees it) rather than leaving that to a raw, host-dependent `ENAMETOOLONG`,
  and `_reject_unless_bare_filename` refuses a drive-relative name
  (`ntpath.splitdrive`) and an embedded NUL outright. A commit-time
  `OSError`/`ValueError` that isn't one of those refusals (a full disk mid-
  `os.replace`, say) is still wrapped as `MediaStoreError`. Every aborted or
  committed upload is now logged — `%r`, not `%s`, since the name comes
  straight from an untrusted upload — where before this module's one
  long-running, network-reachable write left no trace of a failure anywhere
  in `--log-file`.
