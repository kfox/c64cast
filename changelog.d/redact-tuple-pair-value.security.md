- **The log redactor masks the value in a tuple pair.** `('password', 'hunter2')`
  and `[('Set-Cookie', 'a=1; Path=/')]`, which is how `getheaders()` and
  `dict.items()` print, kept the value.
