- **A config or machine-settings file that is not UTF-8 is named in the error.**
  A `settings.toml` or show config saved in another encoding stopped c64cast
  with only "'utf-8' codec can't decode byte …", which said nothing about
  which file was at fault. The error now names the file, says it is not UTF-8,
  and gives the byte offset of the first bad byte.
