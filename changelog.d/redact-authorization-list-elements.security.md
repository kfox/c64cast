- **The log redactor masks the credential of an `--authorization` flag whose scheme
  is its own list element.** `['--authorization', 'Basic', 'abc']` kept `abc`.
