- YouTube URLs no longer fail with "Requested format is not available": the
  `yt` extra now installs yt-dlp's JavaScript challenge solver (`yt-dlp-ejs`),
  and c64cast lets yt-dlp run it with deno, Node.js, Bun or QuickJS, whichever
  is installed. `--doctor` warns when the solver or a JavaScript runtime is
  missing. Reinstall with `uv tool install --force "c64cast[all]"` to pick it
  up.
