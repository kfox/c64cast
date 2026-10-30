- **A launcher scene can no longer be paused by the program it runs.** A
  program keeps its own bytes where c64cast reads the Commodore, CTRL and
  SHIFT keys, and one could set the Commodore bit and pause the playlist,
  which stopped the idle timeout and `max_duration_s` so the scene never
  ended. While a launcher scene runs, those three keys now do nothing: the
  program owns the keyboard. The idle timeout, `max_duration_s`, MIDI and the
  web console still end or skip the scene, and a pause started from MIDI or
  the web console still resumes on a 3-second Commodore-key hold.
