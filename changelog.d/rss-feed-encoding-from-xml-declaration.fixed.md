- **An RSS ticker keeps curly quotes and accents from a UTF-8 feed served
  without a charset.** A feed sent as `text/xml` with no `charset` used to be
  decoded as Latin-1, so `It’s` scrolled as mojibake; the feed's own XML
  declaration now decides its encoding.
