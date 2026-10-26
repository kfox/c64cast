- **A throttled warning shows again the next time its condition recurs after a
  flood.** An ASID host or MIDI controller that set one off many times in a row
  left the count of its last second's repeats pending, and the next occurrence,
  however much later, went out at DEBUG and was hidden at the default log level.
  An occurrence after a quiet second now goes out at WARNING (ERROR for a
  traceback), carrying that count.
