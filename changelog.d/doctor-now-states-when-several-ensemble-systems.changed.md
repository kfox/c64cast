- `--doctor` now states when several ensemble systems authenticate with one
  shared `[ultimate64].dma_password`, naming the systems it reached. Unlike
  `url`, `dma_password` cascades from the master, which is deliberate — the
  alternative copies the same secret into every per-system file, and the master
  is the one place the serializer already refuses to write it — but it is
  invisible from any single config file, so an operator reading only a
  per-system TOML could not see where the password came from. Reported at `ok`
  level, grouped by value so two systems that each named their own different
  password are not described as sharing one, and never quoting the password.
