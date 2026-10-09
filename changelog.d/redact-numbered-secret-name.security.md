- **The log redactor masks a numbered secret name.** `password2 = hunter2`,
  `token_1=…` and `authorization2: …` kept their values in `--log-file`, the web
  console's log tail and the scene snapshot, because a digit after the name
  hid it. `key2` and `sig2` stay readable: they are as likely a column or an
  index.
