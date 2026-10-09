- `_midi.open_input_port`'s only guard against a missing `midi` extra was a
  bare `assert mido is not None`, stripped entirely under `python -O` and
  otherwise surfacing as `AttributeError: 'NoneType' object has no attribute
  'get_input_names'` — a message naming nothing about the extra a caller
  forgot to check. It now raises `RuntimeError` naming the install command,
  matching the contract every other precondition on this shared resolver
  already documents. New `tests/test_midi.py`.
