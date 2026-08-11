# Architecture — per-module internals

This is the per-module reference for the `c64cast/` tree: the design rationale, hardware constraints, and edge-case history behind each module — the *why*, and the dead ends, that the code alone doesn't carry. Read the relevant section before modifying a module, and update it in the same change set when you change that module's behavior.

The reference is split by topic area below. Each `##` section within a topic file covers one module, or a cluster of closely-related modules. Since 2026-08 the package tree mirrors these topic areas on disk — one subpackage per area (`hw/`, `audio/`, `video/`, `scenes/`, `sid/`, `control/`, `wled/`, `app/`), with only the entry point and three private cross-cutting utilities (`_pollthread.py`, `_native_io.py`, `_midi.py`) at the package root. Section headings keep the module's bare filename, so anchors predate — and survive — the move.

For end-user configuration see [the Programmer’s Reference Guide](reference/README.md), for known limitations [caveats.md](caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](extending.md).

## Topic areas

* **[Hardware I/O & transports](architecture/hardware-io.md)** — `hw/backend.py`, `hw/api.py`, `hw/teensyrom_api.py`, `hw/teensyrom_dma.py`, Startup: BASIC clear-and-loop program, `hw/char_rom.py`
* **[Audio output](architecture/audio.md)** — `audio/audio.py`, `audio/audio_handlers.py`, `audio/sampler.py`, `audio/dsp.py`, `audio/audio_features.py`
* **[Video input & the color pipeline](architecture/video-color.md)** — `video/video.py`, `video/modes/`, `video/modes_irq.py`, `video/rolling_palette.py`, `video/palette.py`, Framerate pacing & frame-dropping, `video/framebuffer.py`, `video/preview.py`
* **[Scenes, sources & overlays](architecture/scenes.md)** — `scenes/scenes.py`, Composable scenes, `scenes/overlays/`, `scenes/interstitial.py`, `scenes/backgrounds.py`
* **[SID playback & the oscilloscope](architecture/sid.md)** — `sid/voice_scope.py`, SID player PRG, `sid/waveform.py`, `sid/sidemu.py`, `sid/sid_host_emu.py`, `sid/sid_panning.py`, `sid/sid_volume.py`, `sid/sid_resolved.py`, `sid/midi_scene.py`, `sid/asid.py`, `sid/asid_scene.py`
* **[Control surfaces & live performance](architecture/control.md)** — `control/keyboard.py`, `control/camera.py`, `control/vision.py`, `control/control_plane.py`, `control/midi_control.py`, `control/tempo.py`, `control/performance.py`, `control/perf_console.py`, `control/transport.py`, `control/midi_setup.py`
* **[WLED bridge](architecture/wled.md)** — `wled/wled_sync.py`, `wled/wled_device.py`, `wled/wled_sink.py`
* **[Config, CLI & ensemble](architecture/config.md)** — `app/ensemble.py`, `app/orchestrator.py`, `app/orchestrators/`, `app/paths.py`, `app/config.py`, `app/introspect.py`, `app/scene_factory.py`, `app/cli.py`, `app/doctor.py`, `app/playlist.py`, `app/recording_metadata.py`

## Module index

Alphabetically, and where its notes live. Modules the reference does not cover
yet are listed under [Not covered here](#not-covered-here) below; between them
the two lists account for every module in the tree.

| Module | Notes |
| --- | --- |
| `_midi.py` | [Config, CLI & ensemble](architecture/config.md#_midipy--the-guarded-mido-import) |
| `_native_io.py` | [Config, CLI & ensemble](architecture/config.md#_native_iopy--fd-level-stderr-muting) |
| `_pollthread.py` | [Config, CLI & ensemble](architecture/config.md#_pollthreadpy--the-background-loop-idiom) |
| `hw/api.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| `sid/asid.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `sid/asid_player.py` | [SID playback & the oscilloscope](architecture/sid.md#asid_playerpy--buffered-c64-side-ring-player) |
| `sid/asid_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `sid/asid_sidmap.py` | [SID playback & the oscilloscope](architecture/sid.md#multi-sid-on-the-u64-asid_sidmappy) |
| `audio/audio.py` | [Audio output](architecture/audio.md#audiopy--audiostreamer) |
| `audio/audio_rate.py` | [Audio output](architecture/audio.md#audiopy--audiostreamer) |
| `audio/audio_handlers.py` | [Audio output](architecture/audio.md#audio_handlerspy--the-6502-machine-code-layer) |
| `audio/audio_features.py` | [Audio output](architecture/audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input) |
| `audio/audio_marker.py` | [Audio output](architecture/audio.md#audio_markerpy--the-capture-alignment-marker) |
| `audio/audio_source.py` | [Audio output](architecture/audio.md#audio_sourcepy--audiofilesource-audio-file-reactive-source) |
| `hw/backend.py` | [Hardware I/O & transports](architecture/hardware-io.md#backendpy--the-c64backend-duck-type-hardware-profiles-and-the-shared-write-path) |
| `scenes/backgrounds.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `scenes/bitmap_text.py` | [Scenes, sources & overlays](architecture/scenes.md#bitmap_textpy--the-shared-glyph-rasterizer) |
| `hw/c64.py` | [Hardware I/O & transports](architecture/hardware-io.md#c64py--the-hardware-constant-register) |
| `control/camera.py` | [Control surfaces & live performance](architecture/control.md#camerapy--camera-enumeration--namevidpid-device-selection-optional-camera-extra) |
| `hw/char_rom.py` | [Hardware I/O & transports](architecture/hardware-io.md#char_rompy--reading-the-character-rom-off-the-machine) |
| `app/cli.py` | [Config, CLI & ensemble](architecture/config.md#clipy) |
| `app/cli_commands.py` | [Config, CLI & ensemble](architecture/config.md#clipy) |
| Composable scenes | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generators--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `app/config.py` | [Config, CLI & ensemble](architecture/config.md#configpy) |
| `app/config_serialize.py` | [Config, CLI & ensemble](architecture/config.md#config_serializepy--the-writing-surface) |
| `app/connect.py` | [Config, CLI & ensemble](architecture/config.md#connectpy--scheme-aware-connection-targets) |
| `control/control_plane.py` | [Control surfaces & live performance](architecture/control.md#control_planepy--http-control-plane-optional) |
| `audio/dac_calibration.py` | [Audio output](architecture/audio.md#table-selection-auto-and-per-system-calibration) |
| `audio/dac_calibration_store.py` | [Audio output](architecture/audio.md#the-calibration-file) |
| `audio/dac_capture_device.py` | [Audio output](architecture/audio.md#picking-the-capture-device) |
| `audio/dac_curve_resolve.py` | [Audio output](architecture/audio.md#table-selection-auto-and-per-system-calibration) |
| `audio/dac_curves.py` | [Audio output](architecture/audio.md#audiodac_curve--mahoney-8-bit-d418-companding) |
| `audio/dac_slot_ring.py` | [Audio output](architecture/audio.md#the-slot-ring-reading-signed-levels-directly) |
| `app/doctor.py` | [Config, CLI & ensemble](architecture/config.md#doctorpy--config-and-environment-diagnostics) |
| `video/dither.py` | [Video input & the color pipeline](architecture/video-color.md#colordither--spatial-dither) |
| `audio/dsp.py` | [Audio output](architecture/audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path) |
| `scenes/effects.py` | [Scenes, sources & overlays](architecture/scenes.md#effectspy--the-frameeffect-registry) |
| `app/ensemble.py` | [Config, CLI & ensemble](architecture/config.md#ensemblepy--audio-slot-coordination) |
| `scenes/frame_source.py` | [Scenes, sources & overlays](architecture/scenes.md#frame_sourcepy) |
| `video/framebuffer.py` | [Video input & the color pipeline](architecture/video-color.md#framebufferpy--previewpy--the-software-mirror-behind-preview-and-recording) |
| Framerate pacing & frame-dropping | [Video input & the color pipeline](architecture/video-color.md#framerate-pacing--frame-dropping) |
| `scenes/generators/` | [Scenes, sources & overlays](architecture/scenes.md#generators--the-generativesource-registry) |
| `hw/hw_provision.py` | [Hardware I/O & transports](architecture/hardware-io.md#hw_provisionpy--live-reu--sampler-auto-provisioning) |
| `scenes/interstitial.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `app/introspect.py` | [Config, CLI & ensemble](architecture/config.md#introspectpy--the-model-and-the-terminal-renderers) |
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
| `app/playlist.py` | [Config, CLI & ensemble](architecture/config.md#playlistpy--the-run-loop-scene-walk-pacing-crash-tolerance) |
| `app/playlist_support.py` | [Config, CLI & ensemble](architecture/config.md#playlist_supportpy--playlist-collaborators) |
| `video/preview.py` | [Video input & the color pipeline](architecture/video-color.md#framebufferpy--previewpy--the-software-mirror-behind-preview-and-recording) |
| `app/profiler.py` | [Config, CLI & ensemble](architecture/config.md#profilerpy--per-frame-timing) |
| `app/quickcast.py` | [Config, CLI & ensemble](architecture/config.md#quickcastpy--quick-playback) |
| `app/recording_metadata.py` | [Config, CLI & ensemble](architecture/config.md#recording_metadatapy--per-scene-scene_config_json-logging) |
| `video/rolling_palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `audio/sampler.py` | [Audio output](architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm) |
| `app/scene_factory.py` | [Config, CLI & ensemble](architecture/config.md#scene_factorypy) |
| `scenes/scenes.py` | [Scenes, sources & overlays](architecture/scenes.md#scenespy--scene-state-machine) |
| `scenes/setup_progress.py` | [Scenes, sources & overlays](architecture/scenes.md#setup_progresspy--the-video-setup-progress-bar) |
| `app/schema.py` | [Config, CLI & ensemble](architecture/config.md#schemapy--the-editor-surface) |
| `sid/emusid_mixer.py` | [SID playback & the oscilloscope](architecture/sid.md#emusid_mixerpy--u2-emulated-stereo-sid-snoop-routing) |
| `sid/sid_autoconfig.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-player-autoconfig) |
| SID player PRG | [SID playback & the oscilloscope](architecture/sid.md#sid-player-prg--6502-player-relocation-and-per-call-banking) |
| `sid/sid_host_emu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `sid/sid_hw_config.py` | [SID playback & the oscilloscope](architecture/sid.md#sid_hw_configpy--shared-sid-hardware-config-plumbing) |
| `sid/sid_panning.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-panning) |
| `sid/sid_resolved.py` | [SID playback & the oscilloscope](architecture/sid.md#sid_resolvedpy--the-resolved-audio-line) |
| `sid/sid_volume.py` | [SID playback & the oscilloscope](architecture/sid.md#sid-volume) |
| `sid/sidemu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `hw/socket_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| `sid/songlengths.py` | [SID playback & the oscilloscope](architecture/sid.md#songlengthspy--hvsc-songlengths-lookup) |
| Startup: BASIC clear-and-loop program | [Hardware I/O & transports](architecture/hardware-io.md#startup-basic-clear-and-loop-program) |
| `hw/teensyrom_api.py` | [Hardware I/O & transports](architecture/hardware-io.md#teensyrom_apipy--the-teensyrom-backend) |
| `hw/teensyrom_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race) |
| `scenes/text_surface.py` | [Scenes, sources & overlays](architecture/scenes.md#overlays) |
| `control/transport.py` | [Control surfaces & live performance](architecture/control.md#transportpy--live-tune-tracker--save-back-phase-1--dj-transport-engine-phase-2--record-workflow--loop-presets-phase-3--controller-profiles-phase-5) |
| `video/video.py` | [Video input & the color pipeline](architecture/video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) |
| `scenes/video_transport.py` | [Scenes, sources & overlays](architecture/scenes.md#videoscenes-transport-surface-midi-live-tune-phase-2) |
| `control/vision.py` | [Control surfaces & live performance](architecture/control.md#visionpy--webcam-gesture-control-optional-camera-as-input) |
| `sid/voice_scope.py` | [SID playback & the oscilloscope](architecture/sid.md#voice_scopepy--shared-3-voice-oscilloscope-renderer) |
| `sid/waveform.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `app/wizard.py` | [Config, CLI & ensemble](architecture/config.md#wizardpy--the-prompting-surface) |
| `wled/wled_device.py` | [WLED bridge](architecture/wled.md#wled_devicepy--virtual-wled-device--control-surface-wled-bridge-mode-1) |
| `wled/wled_sink.py` | [WLED bridge](architecture/wled.md#wled_sinkpy--virtual-led-matrix--realtime-pixel-sink-wled-bridge-mode-2) |
| `wled/wled_sync.py` | [WLED bridge](architecture/wled.md#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3) |

## Not covered here

These modules have no section. A module listed here must open with a docstring
carrying its design rationale instead — except the entry point, which is three
lines with nothing to say.

| Module | What it is |
| --- | --- |
| `__main__.py` | `python -m c64cast` entry point |
