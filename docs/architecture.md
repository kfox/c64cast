# Architecture — per-module internals

This is the per-module reference for the `c64cast/` tree: the design rationale, hardware constraints, and edge-case history behind each module — the *why*, and the dead ends, that the code alone doesn't carry. Read the relevant section before modifying a module, and update it in the same change set when you change that module's behavior.

The reference is split by topic area below. Each `##` section within a topic file covers one module, or a cluster of closely-related modules.

For end-user configuration see [the Programmer’s Reference Guide](reference/README.md), for known limitations [caveats.md](caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](extending.md).

## Topic areas

* **[Hardware I/O & transports](architecture/hardware-io.md)** — `api.py`, `teensyrom_dma.py`, Startup: BASIC clear-and-loop program, `char_rom.py`
* **[Audio output](architecture/audio.md)** — `audio.py`, `sampler.py`, `dsp.py`, `audio_features.py`
* **[Video input & the color pipeline](architecture/video-color.md)** — `video.py`, `modes.py`, `rolling_palette.py`, `palette.py`, Framerate pacing & frame-dropping
* **[Scenes, sources & overlays](architecture/scenes.md)** — `scenes.py`, Composable scenes, `overlays/`, `interstitial.py`, `backgrounds.py`
* **[SID playback & the oscilloscope](architecture/sid.md)** — `voice_scope.py`, SID player PRG, `waveform.py`, `sidemu.py`, `sid_host_emu.py`, `sid_panning.py`, `sid_volume.py`, `midi_scene.py`, `asid.py`, `asid_scene.py`
* **[Control surfaces & live performance](architecture/control.md)** — `keyboard.py`, `camera.py`, `vision.py`, `control_plane.py`, `midi_control.py`, `tempo.py`, `performance.py`, `perf_console.py`, `transport.py`, `midi_setup.py`
* **[WLED bridge](architecture/wled.md)** — `wled_sync.py`, `wled_device.py`, `wled_sink.py`
* **[Config, CLI & ensemble](architecture/config.md)** — `ensemble.py`, `orchestrator.py`, `orchestrators/`, `paths.py`, `config.py`, `cli.py`, `recording_metadata.py`

## Module index

Alphabetically, and where its notes live. Modules the reference does not cover
yet are listed under [Not covered here](#not-covered-here) below; between them
the two lists account for every module in the tree.

| Module | Notes |
| --- | --- |
| `api.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| `asid.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `asid_player.py` | [SID playback & the oscilloscope](architecture/sid.md#asid_playerpy--buffered-c64-side-ring-player) |
| `asid_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `asid_sidmap.py` | [SID playback & the oscilloscope](architecture/sid.md#multi-sid-on-the-u64-asid_sidmappy) |
| `audio.py` | [Audio output](architecture/audio.md#audiopy--audiostreamer) |
| `audio_features.py` | [Audio output](architecture/audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input) |
| `audio_source.py` | [Audio output](architecture/audio.md#audio_sourcepy--audiofilesource-audio-file-reactive-source) |
| `backgrounds.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `camera.py` | [Control surfaces & live performance](architecture/control.md#camerapy--camera-enumeration--namevidpid-device-selection-optional-camera-extra) |
| `char_rom.py` | [Hardware I/O & transports](architecture/hardware-io.md#char_rompy--reading-the-character-rom-off-the-machine) |
| `cli.py` | [Config, CLI & ensemble](architecture/config.md#clipy) |
| Composable scenes | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generatorspy--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `config.py` | [Config, CLI & ensemble](architecture/config.md#configpy) |
| `control_plane.py` | [Control surfaces & live performance](architecture/control.md#control_planepy--http-control-plane-optional) |
| `dac_calibration.py` | [Audio output](architecture/audio.md#table-selection-auto-and-per-system-calibration) |
| `dac_curves.py` | [Audio output](architecture/audio.md#audiodac_curve--mahoney-8-bit-d418-companding) |
| `dither.py` | [Video input & the color pipeline](architecture/video-color.md#colordither--spatial-dither) |
| `dsp.py` | [Audio output](architecture/audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path) |
| `effects.py` | [Scenes, sources & overlays](architecture/scenes.md#effectspy--the-frameeffect-registry) |
| `ensemble.py` | [Config, CLI & ensemble](architecture/config.md#ensemblepy--audio-slot-coordination) |
| `frame_source.py` | [Scenes, sources & overlays](architecture/scenes.md#frame_sourcepy) |
| Framerate pacing & frame-dropping | [Video input & the color pipeline](architecture/video-color.md#framerate-pacing--frame-dropping) |
| `generators.py` | [Scenes, sources & overlays](architecture/scenes.md#generatorspy--the-generativesource-registry) |
| `interstitial.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `keyboard.py` | [Control surfaces & live performance](architecture/control.md#keyboardpy--commodore-key-pauseresume-ctrl-key-skip-shift-key-style-cycle) |
| `midi_control.py` | [Control surfaces & live performance](architecture/control.md#midi_controlpy--process-wide-midi-control-surface-optional-live-performance) |
| `midi_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#midi_scenepy--midiscene-live-midi--sid--oscilloscope) |
| `midi_setup.py` | [Control surfaces & live performance](architecture/control.md#midi_setuppy--the---midi-setup-midi-learn-wizard-phase-5) |
| `modulation.py` | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generatorspy--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `music_features.py` | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generatorspy--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `tempo.py` | [Control surfaces & live performance](architecture/control.md#tempopy--process-wide-musical-beat-grid-live-djvj-phase-1) |
| `performance.py` | [Control surfaces & live performance](architecture/control.md#performancepy--clip-launch-grid-live-djvj-phase-2) |
| `perf_console.py` | [Control surfaces & live performance](architecture/control.md#perf_consolepy--phone--web-performance-console-live-djvj-phase-5) |
| `modes.py` | [Video input & the color pipeline](architecture/video-color.md#modespy--displaymode-hierarchy) |
| `orchestrator.py` | [Config, CLI & ensemble](architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
| `orchestrators/` | [Config, CLI & ensemble](architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
| `overlays/` | [Scenes, sources & overlays](architecture/scenes.md#overlays) |
| `palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `paths.py` | [Config, CLI & ensemble](architecture/config.md#pathspy) |
| `petscii_styles.py` | [Video input & the color pipeline](architecture/video-color.md#petscii_stylespy) |
| `recording_metadata.py` | [Config, CLI & ensemble](architecture/config.md#recording_metadatapy--per-scene-scene_config_json-logging) |
| `rolling_palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `sampler.py` | [Audio output](architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm) |
| `scenes.py` | [Scenes, sources & overlays](architecture/scenes.md#scenespy--scene-state-machine) |
| `sid_autoconfig.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-player-autoconfig) |
| SID player PRG | [SID playback & the oscilloscope](architecture/sid.md#sid-player-prg--6502-player-relocation-and-per-call-banking) |
| `sid_host_emu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `sid_panning.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-panning) |
| `sid_volume.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-volume) |
| `sidemu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `socket_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| Startup: BASIC clear-and-loop program | [Hardware I/O & transports](architecture/hardware-io.md#startup-basic-clear-and-loop-program) |
| `teensyrom_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race) |
| `text_surface.py` | [Scenes, sources & overlays](architecture/scenes.md#overlays) |
| `transport.py` | [Control surfaces & live performance](architecture/control.md#transportpy--live-tune-tracker--save-back-phase-1--dj-transport-engine-phase-2--record-workflow--loop-presets-phase-3--controller-profiles-phase-5) |
| `video.py` | [Video input & the color pipeline](architecture/video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) |
| `vision.py` | [Control surfaces & live performance](architecture/control.md#visionpy--webcam-gesture-control-optional-camera-as-input) |
| `voice_scope.py` | [SID playback & the oscilloscope](architecture/sid.md#voice_scopepy--shared-3-voice-oscilloscope-renderer) |
| `waveform.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `wled_device.py` | [WLED bridge](architecture/wled.md#wled_devicepy--virtual-wled-device--control-surface-wled-bridge-mode-1) |
| `wled_sink.py` | [WLED bridge](architecture/wled.md#wled_sinkpy--virtual-led-matrix--realtime-pixel-sink-wled-bridge-mode-2) |
| `wled_sync.py` | [WLED bridge](architecture/wled.md#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3) |

## Not covered here

These modules have no section yet. Their module docstring is the design
rationale in the meantime — each of the ones below opens with one.

| Module | What it is |
| --- | --- |
| `__main__.py` | `python -m c64cast` entry point |
| `_native_io.py` | Process-level stderr muting for native-library chatter |
| `_pollthread.py` | Background daemon thread with start/stop boilerplate |
| `audio_marker.py` | Source-timeline alignment marker for capture-card recordings |
| `backend.py` | The `C64Backend` hardware abstraction the whole app is duck-typed on |
| `bitmap_text.py` | Shared hires bitmap text rasterizer (char-ROM glyphs) |
| `c64.py` | Centralized C64 hardware constants — addresses, registers, magic numbers |
| `config_serialize.py` | `Config` → annotated TOML, the inverse of `config.load` |
| `connect.py` | `-u/--url` connection-target URI parsing |
| `doctor.py` | `--doctor` configuration + environment diagnostics |
| `framebuffer.py` | Software VIC-II framebuffer behind preview + recording |
| `introspect.py` | The single rendering surface over config metadata |
| `playlist.py` | Playlist state machine — scene walk, pacing, crash tolerance |
| `preview.py` | `PreviewWindow` + `StreamRecorder` over the framebuffer |
| `profiler.py` | `--profile` per-frame timing harness |
| `quickcast.py` | Positional-`MEDIA` quick-playback config builder |
| `schema.py` | JSON Schema generator for the TOML config |
| `sid_hw_config.py` | Shared U64 multi-SID hardware-config snapshot/restore |
| `songlengths.py` | HVSC `Songlengths.md5` lookup |
| `teensyrom_api.py` | TeensyROM+ implementation of `C64Backend` |
| `wizard.py` | `--init` interactive config builder |
