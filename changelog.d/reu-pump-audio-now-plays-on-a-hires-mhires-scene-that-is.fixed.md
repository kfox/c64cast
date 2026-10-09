- **REU-pump audio now plays on a `hires`/`mhires` scene that is not
  REU-staged.** With `[audio].use_reu_pump` on and `use_reu_staged` resolved
  off (`false`, a text overlay under `"auto"`, or `--skip-probe`), the pump
  left `$0314` alone for a dispatcher that only the REU-staged path installs,
  so nothing ever ran it. It now hooks `$0314` itself there.
