- **A `[web] token_file` that is not UTF-8 is named in the error.** It used to
  stop `c64cast serve` with a bare decoding error that named neither the file
  nor the setting; it now says which file is not UTF-8 text, without quoting
  any of the secret it holds.
