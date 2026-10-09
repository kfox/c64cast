- **The log redactor masks the whole value of an `--authorization` flag.**
  `--authorization Basic abc` kept the credential, because a flag's value was
  one word and an `Authorization` value is a scheme and a credential.
