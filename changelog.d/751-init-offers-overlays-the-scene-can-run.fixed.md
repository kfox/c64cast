- **`--init` offers a scope or launcher scene only the overlays it can run.**
  A waveform, midi or asid scene was offered overlays as if it painted PETSCII,
  so the wizard could add one the config check then rejected, and a launcher
  scene was offered overlays it never draws. The scope scenes now get the
  overlays that work on the hires bitmap they paint, and a launcher gets none.
