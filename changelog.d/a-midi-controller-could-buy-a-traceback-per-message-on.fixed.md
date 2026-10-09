- **A MIDI controller could buy a traceback per message on the control
  surface.** `midi_control`'s reader, clock reader, and per-system action
  dispatch each logged a full traceback every time a message failed — which,
  for a held pad or a swept controller against a mapping this build mishandles,
  is once per message at the controller's rate, on the thread the next pad
  press waits behind. The same throttle the ASID wire got now bounds all three
  to one report per second per site: the first at ERROR with the traceback as
  before, repeats counted and folded into the next report. Nothing that used to
  reach the log at ERROR is lost — the first occurrence is always emitted.
