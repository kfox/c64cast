# Architecture — per-module internals

This is the per-module reference for the `c64cast/` tree: the design rationale, hardware constraints, and edge-case history behind each module — the *why*, and the dead ends, that the code alone doesn't carry. Read the relevant section before modifying a module, and update it in the same change set when you change that module's behavior.

The reference is split by topic area below. Each `##` section within a topic file covers one module, or a cluster of closely-related modules. Since 2026-08 the package tree mirrors these topic areas on disk — one subpackage per area (`hw/`, `audio/`, `video/`, `scenes/`, `sid/`, `control/`, `wled/`, `app/`), with only the entry point and three private cross-cutting utilities (`_pollthread.py`, `_native_io.py`, `_midi.py`) at the package root. Section headings keep the module's bare filename, so anchors predate — and survive — the move.

For end-user configuration see [the Programmer’s Reference Guide](reference/README.md), for known limitations [caveats.md](caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](extending.md).

## Topic areas

* **[Hardware I/O & transports](architecture/hardware-io.md)** — `hw/api.py`, `hw/teensyrom_dma.py`, Startup: BASIC clear-and-loop program, `hw/char_rom.py`
* **[Audio output](architecture/audio.md)** — `audio/audio.py`, `audio/audio_handlers.py`, `audio/sampler.py`, `audio/dsp.py`, `audio/audio_features.py`
* **[Video input & the color pipeline](architecture/video-color.md)** — `video/video.py`, `video/modes/`, `video/modes_irq.py`, `video/rolling_palette.py`, `video/palette.py`, Framerate pacing & frame-dropping
* **[Scenes, sources & overlays](architecture/scenes.md)** — `scenes/scenes.py`, Composable scenes, `scenes/overlays/`, `scenes/interstitial.py`, `scenes/backgrounds.py`
* **[SID playback & the oscilloscope](architecture/sid.md)** — `sid/voice_scope.py`, SID player PRG, `sid/waveform.py`, `sid/sidemu.py`, `sid/sid_host_emu.py`, `sid/sid_panning.py`, `sid/sid_volume.py`, `sid/midi_scene.py`, `sid/asid.py`, `sid/asid_scene.py`
* **[Control surfaces & live performance](architecture/control.md)** — `control/keyboard.py`, `control/camera.py`, `control/vision.py`, `control/control_plane.py`, `control/midi_control.py`, `control/tempo.py`, `control/performance.py`, `control/perf_console.py`, `control/transport.py`, `control/midi_setup.py`
* **[WLED bridge](architecture/wled.md)** — `wled/wled_sync.py`, `wled/wled_device.py`, `wled/wled_sink.py`
* **[Config, CLI & ensemble](architecture/config.md)** — `app/ensemble.py`, `app/orchestrator.py`, `app/orchestrators/`, `app/paths.py`, `app/config.py`, `app/scene_factory.py`, `app/cli.py`, `app/recording_metadata.py`

## Module index

Alphabetically, and where its notes live. Modules the reference does not cover
yet are listed under [Not covered here](#not-covered-here) below; between them
the two lists account for every module in the tree.

| Module | Notes |
| --- | --- |
| `hw/api.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| `sid/asid.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `sid/asid_player.py` | [SID playback & the oscilloscope](architecture/sid.md#asid_playerpy--buffered-c64-side-ring-player) |
| `sid/asid_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `sid/asid_sidmap.py` | [SID playback & the oscilloscope](architecture/sid.md#multi-sid-on-the-u64-asid_sidmappy) |
| `audio/audio.py` | [Audio output](architecture/audio.md#audiopy--audiostreamer) |
| `audio/audio_rate.py` | [Audio output](architecture/audio.md#audiopy--audiostreamer) |
| `audio/audio_handlers.py` | [Audio output](architecture/audio.md#audio_handlerspy--the-6502-machine-code-layer) |
| `audio/audio_features.py` | [Audio output](architecture/audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input) |
| `audio/audio_source.py` | [Audio output](architecture/audio.md#audio_sourcepy--audiofilesource-audio-file-reactive-source) |
| `scenes/backgrounds.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `control/camera.py` | [Control surfaces & live performance](architecture/control.md#camerapy--camera-enumeration--namevidpid-device-selection-optional-camera-extra) |
| `hw/char_rom.py` | [Hardware I/O & transports](architecture/hardware-io.md#char_rompy--reading-the-character-rom-off-the-machine) |
| `app/cli.py` | [Config, CLI & ensemble](architecture/config.md#clipy) |
| `app/cli_commands.py` | [Config, CLI & ensemble](architecture/config.md#clipy) |
| Composable scenes | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generators--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `app/config.py` | [Config, CLI & ensemble](architecture/config.md#configpy) |
| `control/control_plane.py` | [Control surfaces & live performance](architecture/control.md#control_planepy--http-control-plane-optional) |
| `audio/dac_calibration.py` | [Audio output](architecture/audio.md#table-selection-auto-and-per-system-calibration) |
| `audio/dac_calibration_store.py` | [Audio output](architecture/audio.md#the-calibration-file) |
| `audio/dac_capture_device.py` | [Audio output](architecture/audio.md#picking-the-capture-device) |
| `audio/dac_curve_resolve.py` | [Audio output](architecture/audio.md#table-selection-auto-and-per-system-calibration) |
| `audio/dac_curves.py` | [Audio output](architecture/audio.md#audiodac_curve--mahoney-8-bit-d418-companding) |
| `audio/dac_slot_ring.py` | [Audio output](architecture/audio.md#the-slot-ring-reading-signed-levels-directly) |
| `video/dither.py` | [Video input & the color pipeline](architecture/video-color.md#colordither--spatial-dither) |
| `audio/dsp.py` | [Audio output](architecture/audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path) |
| `scenes/effects.py` | [Scenes, sources & overlays](architecture/scenes.md#effectspy--the-frameeffect-registry) |
| `app/ensemble.py` | [Config, CLI & ensemble](architecture/config.md#ensemblepy--audio-slot-coordination) |
| `scenes/frame_source.py` | [Scenes, sources & overlays](architecture/scenes.md#frame_sourcepy) |
| Framerate pacing & frame-dropping | [Video input & the color pipeline](architecture/video-color.md#framerate-pacing--frame-dropping) |
| `scenes/generators/` | [Scenes, sources & overlays](architecture/scenes.md#generators--the-generativesource-registry) |
| `scenes/interstitial.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `control/keyboard.py` | [Control surfaces & live performance](architecture/control.md#keyboardpy--commodore-key-pauseresume-ctrl-key-skip-shift-key-style-cycle) |
| `control/midi_control.py` | [Control surfaces & live performance](architecture/control.md#midi_controlpy--process-wide-midi-control-surface-optional-live-performance) |
| `sid/midi_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#midi_scenepy--midiscene-live-midi--sid--oscilloscope) |
| `control/midi_setup.py` | [Control surfaces & live performance](architecture/control.md#midi_setuppy--the---midi-setup-midi-learn-wizard-phase-5) |
| `scenes/modulation.py` | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generators--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `scenes/music_features.py` | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generators--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `control/tempo.py` | [Control surfaces & live performance](architecture/control.md#tempopy--process-wide-musical-beat-grid-live-djvj-phase-1) |
| `control/performance.py` | [Control surfaces & live performance](architecture/control.md#performancepy--clip-launch-grid-live-djvj-phase-2) |
| `control/perf_console.py` | [Control surfaces & live performance](architecture/control.md#perf_consolepy--phone--web-performance-console-live-djvj-phase-5) |
| `video/modes/` | [Video input & the color pipeline](architecture/video-color.md#modes--displaymode-hierarchy) |
| `video/modes_irq.py` | [Video input & the color pipeline](architecture/video-color.md#modes_irqpy--c64-side-irq-handlers--reu-push-helpers) |
| `app/orchestrator.py` | [Config, CLI & ensemble](architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
| `app/orchestrators/` | [Config, CLI & ensemble](architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
| `scenes/overlays/` | [Scenes, sources & overlays](architecture/scenes.md#overlays) |
| `video/palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `app/paths.py` | [Config, CLI & ensemble](architecture/config.md#pathspy) |
| `video/petscii_styles.py` | [Video input & the color pipeline](architecture/video-color.md#petscii_stylespy) |
| `app/recording_metadata.py` | [Config, CLI & ensemble](architecture/config.md#recording_metadatapy--per-scene-scene_config_json-logging) |
| `video/rolling_palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `audio/sampler.py` | [Audio output](architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm) |
| `app/scene_factory.py` | [Config, CLI & ensemble](architecture/config.md#scene_factorypy) |
| `scenes/scenes.py` | [Scenes, sources & overlays](architecture/scenes.md#scenespy--scene-state-machine) |
| `sid/sid_autoconfig.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-player-autoconfig) |
| SID player PRG | [SID playback & the oscilloscope](architecture/sid.md#sid-player-prg--6502-player-relocation-and-per-call-banking) |
| `sid/sid_host_emu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `sid/sid_panning.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-panning) |
| `sid/sid_volume.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-volume) |
| `sid/sidemu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `hw/socket_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| Startup: BASIC clear-and-loop program | [Hardware I/O & transports](architecture/hardware-io.md#startup-basic-clear-and-loop-program) |
| `hw/teensyrom_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race) |
| `scenes/text_surface.py` | [Scenes, sources & overlays](architecture/scenes.md#overlays) |
| `control/transport.py` | [Control surfaces & live performance](architecture/control.md#transportpy--live-tune-tracker--save-back-phase-1--dj-transport-engine-phase-2--record-workflow--loop-presets-phase-3--controller-profiles-phase-5) |
| `video/video.py` | [Video input & the color pipeline](architecture/video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) |
| `scenes/video_transport.py` | [Scenes, sources & overlays](architecture/scenes.md#videoscenes-transport-surface-midi-live-tune-phase-2) |
| `control/vision.py` | [Control surfaces & live performance](architecture/control.md#visionpy--webcam-gesture-control-optional-camera-as-input) |
| `sid/voice_scope.py` | [SID playback & the oscilloscope](architecture/sid.md#voice_scopepy--shared-3-voice-oscilloscope-renderer) |
| `sid/waveform.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `wled/wled_device.py` | [WLED bridge](architecture/wled.md#wled_devicepy--virtual-wled-device--control-surface-wled-bridge-mode-1) |
| `wled/wled_sink.py` | [WLED bridge](architecture/wled.md#wled_sinkpy--virtual-led-matrix--realtime-pixel-sink-wled-bridge-mode-2) |
| `wled/wled_sync.py` | [WLED bridge](architecture/wled.md#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3) |

## Not covered here

These modules have no section yet. Their module docstring is the design
rationale in the meantime — each of the ones below opens with one.

| Module | What it is |
| --- | --- |
| `__main__.py` | `python -m c64cast` entry point |
| `_midi.py` | Shared guarded mido import + MIDI input-port resolution |
| `_native_io.py` | Process-level stderr muting for native-library chatter |
| `_pollthread.py` | Background daemon thread with start/stop boilerplate |
| `audio/audio_marker.py` | Source-timeline alignment marker for capture-card recordings |
| `hw/backend.py` | The `C64Backend` hardware abstraction the whole app is duck-typed on |
| `scenes/bitmap_text.py` | Shared hires bitmap text rasterizer (char-ROM glyphs) |
| `hw/c64.py` | Centralized C64 hardware constants — addresses, registers, magic numbers |
| `app/config_serialize.py` | `Config` → annotated TOML, the inverse of `config.load` |
| `app/connect.py` | `-u/--url` connection-target URI parsing |
| `app/doctor.py` | `--doctor` configuration + environment diagnostics |
| `video/framebuffer.py` | Software VIC-II framebuffer behind preview + recording |
| `hw/hw_provision.py` | Live U64 REU + Ultimate Audio sampler auto-provisioning (volatile, restored at teardown) |
| `app/introspect.py` | The single rendering surface over config metadata |
| `app/playlist.py` | Playlist state machine — scene walk, pacing, crash tolerance |
| `app/playlist_support.py` | Playlist collaborators — scene fades, on-C64 menu driver, ensemble coordination |
| `video/preview.py` | `PreviewWindow` + `StreamRecorder` over the framebuffer |
| `app/profiler.py` | `--profile` per-frame timing harness |
| `app/quickcast.py` | Positional-`MEDIA` quick-playback config builder |
| `app/schema.py` | JSON Schema generator for the TOML config |
| `sid/sid_hw_config.py` | Shared U64 multi-SID hardware-config snapshot/restore + the `SidHwSession` restore tracker |
| `sid/songlengths.py` | HVSC `Songlengths.md5` lookup |
| `hw/teensyrom_api.py` | TeensyROM+ implementation of `C64Backend` |
| `app/wizard.py` | `--init` interactive config builder |
