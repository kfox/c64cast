- **The WLED device page now sends the same hardening headers as the `/perf`
  console.** It went out with no `frame-ancestors` and no `X-Frame-Options` at
  all, which left framing the page as the one cross-site path the `Origin`
  check does not close: a hostile page could frame the device root, overlay a
  decoy, and collect a tap that lands inside the frame as a same-origin
  request. Both pages are assembled by the same helper, so the headers now live
  beside it and cannot diverge again.
