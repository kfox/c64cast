- **A video streamed from a URL no longer hangs the show when the server stops
  answering a seek.** Starting at `start_s`, the loudness scan, the color
  pre-scan and a seek from the transport controls all waited forever on a
  silent server; each now gives up after the 30 s read timeout. A failed start
  seek fails the scene, the loudness scan plays at unity gain, the color
  pre-scan is skipped, and a failed transport seek ends the scene.
