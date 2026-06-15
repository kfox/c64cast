# assets/

Non-code static content used by c64cast at runtime. Subdirectories are tracked
by their `README.md`; the actual files are ignored by `.gitignore` because most
of them have unclear redistribution status (commercial ROMs, copyrighted SIDs,
user-supplied imagery).

Drop your own files in here and reference them from `c64cast.toml`:

| Directory                | Used by                | File types                                                  |
|--------------------------|------------------------|-------------------------------------------------------------|
| [roms/](roms/)           | preview, framebuffer   | `*.bin` (CHARGEN)                                           |
| [sids/](sids/)           | waveform scene         | `*.sid` (HVSC unpack includes `DOCUMENTS/Songlengths.md5`)  |
| [logos/](logos/)         | logo overlay           | `*.txt`                                                     |
| [videos/](videos/)       | video scene       | `*.mp4`, `*.webm`, `*.mkv`…                                 |
| [audio/](audio/)         | DSP A/B test clips     | clean CC/public speech + music (`scripts/diags/dsp_ab.py`)  |
| [pictures/](pictures/)   | slideshow scene        | `*.jpg`, `*.png`, `*.bmp`, `*.webp`                         |
| [programs/](programs/)   | launcher scene         | `*.prg`, `*.crt`                                            |
| [models/](models/)       | vision controller      | `hand_landmarker.task` (MediaPipe HandLandmarker bundle)    |

None of these directories is required — every consumer falls back to a sensible
default (or simply does nothing) when the file is missing.
