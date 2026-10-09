- `--doctor` rebuilt its merged `LoadResult` field-by-field, which silently
  dropped `master_web` (added after this code was written) instead of
  carrying it forward — latent today (nothing in `doctor.py` reads it yet)
  but one new web-related check away from validating the wrong object on
  every ensemble config. Now built with `dataclasses.replace(loaded,
  cfgs=cfgs)`, so a future `LoadResult` field can't be forgotten the same way.
