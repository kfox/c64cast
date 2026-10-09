- `ConsoleLibrary._load` (`console_library.py`) iterated
  `raw.get("favorites", [])`/`raw.get("recents", [])` unguarded —
  `dict.get`'s default only applies when the key is *absent*, so a foreign
  or half-written `console.json` containing `{"favorites": null}` (or a bare
  string or number) raised `TypeError` straight out of `as_dict`,
  contradicting the documented "a missing, corrupt, or wrong-shaped file
  reads as an empty library" contract and taking `GET /api/library` down
  with a 500 instead of self-healing. Both containers are now type-checked
  as lists before iterating.
