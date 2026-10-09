- `make_backend` (`backend.py`) and `resolve_system` (`hw_provision.py`)
  compared `[ultimate64].system` to `"NTSC"`/`"auto"` without normalizing
  case, while every other consumer of the field (`resolve_host_sid_model`,
  `c64.py`, `scene_factory.py`, `music_features.py`) does — nothing at
  config load enforces the canonical spelling, so `system = "ntsc"` used to
  reach `make_backend` intact and pace an NTSC machine at the PAL 50 fps
  with no diagnostic. `BufferedWriteBackend.write_memory` never incremented
  `stats["bytes"]` (only `write_memory_file` did), so every `write_regs`
  register push — the per-frame VIC/`$D418` traffic — was invisible in the
  byte counter the architecture doc's throughput figures are derived from.
  A write listener that failed on every write (a full disk, a stale preview
  widget) logged a full traceback per write, up to the write rate; it now
  follows the same 1st/10th/50th/200th ladder `_emit`'s failure path already
  uses. `BACKENDS`'s comment overclaimed that it "maps the token to its
  base profile" — it's a bare tuple consumed only by the CLI's `--help`
  choices; the real dispatch is `make_backend`'s own `if`/`elif` chain, now
  documented as such. `write_region`'s docstring now states the 16-bit
  address bound it was already relying on callers to honor.
