- The virtual WLED device (`wled_device.py`, bridge Mode 1) accepted a state
  change POSTed from any origin — a page the operator merely had open in a
  browser tab could pause the show, black the screen, or delete presets, with
  no CORS preflight to stop it. `POST /json` and `/json/state` now reject a
  request whose `Origin` header names a different host than its own; a
  same-origin fetch from the served `/` page or a non-browser client
  (python-wled, Home Assistant) is unaffected. Separately, a client posting a
  preset id past 250 used to silently no-op instead of landing on a free
  slot, and omitting `psave` on a full store silently overwrote preset 250 —
  both now go through the same free-slot search, which reports the store as
  full (rather than picking a stale id) once every slot is taken.
