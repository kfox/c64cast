- **`-vv` now does something.** It has always been accepted and has always
  meant exactly what `-v` means: DEBUG is reached at the first `-v`, and no
  code anywhere read a verbosity of 2. It now releases urllib3, whose record
  per HTTP request `-v` holds back at WARNING because it buries everything
  else in the log. No other logger's level moves, so that release is the whole
  of what the second `v` does to the levels. Reach for `-vv` when the question
  is about an Ultimate's REST link itself: a request that never returned, a
  status the application logged only the consequence of. A TeensyROM link
  is serial or raw TCP, so on one of those the second `v` says nothing about
  the link itself.
