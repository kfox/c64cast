- **`--doctor` no longer crashes on a `#:schema` copy that is not UTF-8.** A
  config whose `#:schema` line named a copy of `c64cast.schema.json` saved in
  another encoding ended `--doctor` with a traceback before any report printed.
  The copy is now reported as not matching the schema this install generates.
