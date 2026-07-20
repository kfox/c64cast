# Architecture — per-module internals

This is the per-module reference for the `c64cast/` tree: the design rationale, hardware constraints, and edge-case history behind each module — the *why*, and the dead ends, that the code alone doesn't carry. Read the relevant section before modifying a module, and update it in the same change set when you change that module's behavior.

The reference is split by topic area below. Each `##` section within a topic file covers one module, or a cluster of closely-related modules.

For end-user configuration see [usage.md](usage.md), for known limitations [caveats.md](caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](extending.md).

## Topic areas

* **[Hardware I/O & transports](architecture/hardware-io.md)** — `api.py`, `teensyrom_dma.py`, Startup: BASIC clear-and-loop program
* **[Audio output](architecture/audio.md)** — `audio.py`, `sampler.py`, `dsp.py`
* **[Video input & the color pipeline](architecture/video-color.md)** — `video.py`, `modes.py`, `rolling_palette.py`, `palette.py`, Framerate pacing & frame-dropping
* **[Scenes, sources & overlays](architecture/scenes.md)** — `scenes.py`, Composable scenes, `overlays/`, `interstitial.py`, `backgrounds.py`
* **[SID playback & the oscilloscope](architecture/sid.md)** — `voice_scope.py`, SID player PRG, `waveform.py`, `sidemu.py`, `sid_host_emu.py`, `midi_scene.py`, `asid.py`, `asid_scene.py`
* **[Control surfaces & live performance](architecture/control.md)** — `keyboard.py`, `camera.py`, `vision.py`, `control_plane.py`, `midi_control.py`, `tempo.py`, `transport.py`, `midi_setup.py`
* **[WLED bridge](architecture/wled.md)** — `wled_sync.py`, `wled_device.py`, `wled_sink.py`
* **[Config, CLI & ensemble](architecture/config.md)** — `ensemble.py`, `orchestrator.py`, `orchestrators/`, `paths.py`, `config.py`, `cli.py`, `recording_metadata.py`

## Module index

Every module, alphabetically, and where its notes live.

| Module | Notes |
| --- | --- |
| `api.py` | [Hardware I/O & transports](architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient) |
| `asid.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `asid_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| `audio.py` | [Audio output](architecture/audio.md#audiopy--audiostreamer) |
| `backgrounds.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `camera.py` | [Control surfaces & live performance](architecture/control.md#camerapy--camera-enumeration--namevidpid-device-selection-optional-camera-extra) |
| `cli.py` | [Config, CLI & ensemble](architecture/config.md#clipy) |
| Composable scenes | [Scenes, sources & overlays](architecture/scenes.md#composable-scenes--scenessourcescene--frame_sourcepy--generatorspy--effectspy--audio_sourcepy--modulationpy--music_featurespy) |
| `config.py` | [Config, CLI & ensemble](architecture/config.md#configpy) |
| `control_plane.py` | [Control surfaces & live performance](architecture/control.md#control_planepy--http-control-plane-optional) |
| `dsp.py` | [Audio output](architecture/audio.md#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path) |
| `ensemble.py` | [Config, CLI & ensemble](architecture/config.md#ensemblepy--audio-slot-coordination) |
| Framerate pacing & frame-dropping | [Video input & the color pipeline](architecture/video-color.md#framerate-pacing--frame-dropping) |
| `interstitial.py` | [Scenes, sources & overlays](architecture/scenes.md#interstitialpy--backgroundspy) |
| `keyboard.py` | [Control surfaces & live performance](architecture/control.md#keyboardpy--commodore-key-pauseresume-ctrl-key-skip-shift-key-style-cycle) |
| `midi_control.py` | [Control surfaces & live performance](architecture/control.md#midi_controlpy--process-wide-midi-control-surface-optional-live-performance) |
| `midi_scene.py` | [SID playback & the oscilloscope](architecture/sid.md#midi_scenepy--midiscene-live-midi--sid--oscilloscope) |
| `midi_setup.py` | [Control surfaces & live performance](architecture/control.md#midi_setuppy--the---midi-setup-midi-learn-wizard-phase-5) |
| `tempo.py` | [Control surfaces & live performance](architecture/control.md#tempopy--process-wide-musical-beat-grid-live-djvj-phase-1) |
| `modes.py` | [Video input & the color pipeline](architecture/video-color.md#modespy--displaymode-hierarchy) |
| `orchestrator.py` | [Config, CLI & ensemble](architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
| `orchestrators/` | [Config, CLI & ensemble](architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
| `overlays/` | [Scenes, sources & overlays](architecture/scenes.md#overlays) |
| `palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `paths.py` | [Config, CLI & ensemble](architecture/config.md#pathspy) |
| `recording_metadata.py` | [Config, CLI & ensemble](architecture/config.md#recording_metadatapy--per-scene-scene_config_json-logging) |
| `rolling_palette.py` | [Video input & the color pipeline](architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| `sampler.py` | [Audio output](architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm) |
| `scenes.py` | [Scenes, sources & overlays](architecture/scenes.md#scenespy--scene-state-machine) |
| SID player PRG | [SID playback & the oscilloscope](architecture/sid.md#sid-player-prg--6502-player-relocation-and-per-call-banking) |
| `sid_host_emu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `sidemu.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| Startup: BASIC clear-and-loop program | [Hardware I/O & transports](architecture/hardware-io.md#startup-basic-clear-and-loop-program) |
| `teensyrom_dma.py` | [Hardware I/O & transports](architecture/hardware-io.md#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race) |
| `transport.py` | [Control surfaces & live performance](architecture/control.md#transportpy--live-tune-tracker--save-back-phase-1--dj-transport-engine-phase-2--record-workflow--loop-presets-phase-3--controller-profiles-phase-5) |
| `video.py` | [Video input & the color pipeline](architecture/video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) |
| `vision.py` | [Control surfaces & live performance](architecture/control.md#visionpy--webcam-gesture-control-optional-camera-as-input) |
| `voice_scope.py` | [SID playback & the oscilloscope](architecture/sid.md#voice_scopepy--shared-3-voice-oscilloscope-renderer) |
| `waveform.py` | [SID playback & the oscilloscope](architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| `wled_device.py` | [WLED bridge](architecture/wled.md#wled_devicepy--virtual-wled-device--control-surface-wled-bridge-mode-1) |
| `wled_sink.py` | [WLED bridge](architecture/wled.md#wled_sinkpy--virtual-led-matrix--realtime-pixel-sink-wled-bridge-mode-2) |
| `wled_sync.py` | [WLED bridge](architecture/wled.md#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3) |
