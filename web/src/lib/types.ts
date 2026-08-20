/** Shapes the daemon sends. Hand-written rather than generated: the API is
 *  small, and a generator would be a second thing to keep in sync with the
 *  Python that is already the source of truth. `svelte-check` in `npm run
 *  build` is what catches a screen reading a field that isn't here. */

export type SessionState = "idle" | "starting" | "running" | "stopping" | "error";

/** `SessionStatus.as_dict()` in c64cast/app/serve.py, plus the log cursor
 *  web_api adds to it. */
export interface SessionStatus {
  state: SessionState;
  generation: number;
  config_path: string;
  /** `config_path` as a ref (`<root-label>/<relative>`), or null when that
   *  path isn't under any configured root — a quick-playback run, or one
   *  started from outside `[web].config_roots`. What the browser preselects
   *  and reveals as the running config; there is no "host default" concept
   *  beyond this. */
  config_ref: string | null;
  systems: string[];
  last_error: string | null;
  hardware_wait_s: number;
  log_seq: number;
}

/** One record from `SessionLogBuffer`. */
export interface LogLine {
  seq: number;
  t: number;
  level: string;
  name: string;
  message: string;
  generation: number;
}

/** One `.toml` under one of `[web].config_roots`. `path` is the ref the API
 *  addresses it by — `<root-label>/<relative>`, never a filesystem path. */
export interface ConfigFile {
  path: string;
  root: string;
  /** `path` with the root label stripped — what the list displays (the
   *  `.toml` suffix is stripped by the client, subdirectories kept). */
  rel: string;
  name: string;
  size: number;
  mtime: number;
  /** True under the packaged-examples root: readable and copyable, never
   *  writable or deletable. */
  readonly: boolean;
}

export interface ConfigRoot {
  label: string;
  path: string;
  readonly: boolean;
}

export interface ConfigIndex {
  roots: ConfigRoot[];
  files: ConfigFile[];
  truncated: boolean;
}

/** One entry from `GET /api/media` (`media_store.py`) — a file or a directory
 *  that could go straight into a scene's `file =` field. `spec` is built from
 *  its root exactly as configured (a `~` stays a `~`), not a synthetic ref. */
export interface MediaEntry {
  spec: string;
  name: string;
  is_dir: boolean;
  size: number;
  mtime: number;
}

export interface MediaIndex {
  kind: string;
  roots: string[];
  entries: MediaEntry[];
  truncated: boolean;
}

/** `PUT /api/media/{name}` — what landed on disk. `spec` is the value to
 *  write into the scene's `file =` field, built the same way `MediaEntry.spec`
 *  is. `renamed` is true when `name` was already taken and the upload was
 *  given a `-2`-style name instead — never an overwrite. */
export interface MediaUploaded {
  spec: string;
  name: string;
  kind: string;
  bytes: number;
  renamed: boolean;
}

/** `GET /api/library` — favorites + recently-launched configs, shared across
 *  every browser or phone pointed at this host (`console_library.py`). */
export interface LibraryEntry {
  ref: string;
  at: number;
}

export interface LibraryState {
  favorites: string[];
  recents: LibraryEntry[];
}

/** `GET /api/screen`. `systems` maps a running system's name to whether that
 *  machine can stream its own VIC output — false on an Ultimate II+, which is
 *  a cartridge in someone else's C64, and on a TeensyROM+. Empty when nothing
 *  is running or the host has the screen turned off, which `fps: 0` says. */
export interface ScreenAvailability {
  systems: Record<string, boolean>;
  fps: number;
}

/** `POST /api/viewer-link`. `path` is origin-relative — the host cannot know
 *  which of its addresses this browser reached it on. `minted` is true the
 *  first time, when the token did not exist until it was asked for. */
export interface ViewerLink {
  token: string;
  path: string;
  minted: boolean;
}

/** `"full"` may write; `"viewer"` may only watch. Null when the host runs
 *  without authentication, which `[web]` never does. */
export type Role = "full" | "viewer" | null;

// -- introspection: what the code says a config *may* contain ---------------

/** One field of one config section, from `introspect.as_dict()`. `apply` is
 *  the reason this app reads introspection rather than the committed JSON
 *  schema, which drops it along with `applies_to`. */
export interface FieldDoc {
  name: string;
  type: string;
  default: unknown;
  required: boolean;
  help: string;
  choices: string[];
  /** Scene types this field means anything for; empty is "all of them". */
  applies_to: string[];
  apply: "live" | "rebuild";
  /** The named set this field's string values come from, when it is small
   *  enough to offer whole. `"c64color"` is the sixteen palette entries, which
   *  `choices` cannot say because the field takes an index as well. */
  vocabulary: string;
}

/** One C64 color, from `introspect.palette_swatches()`. `name` is the
 *  spelling to write into a config; `label` the one to show. */
export interface Swatch {
  index: number;
  name: string;
  label: string;
  hex: string;
}

export interface SectionDoc {
  name: string;
  help: string;
  /** Whether a running session's *reload* picks this section up, or it takes a
   *  restart. `FieldDoc.apply` answers the narrower question of whether a
   *  change lands without even a scene rebuild; this is the one the form has to
   *  answer at the moment somebody saves. */
  reload: boolean;
  fields: FieldDoc[];
}

export interface SceneTypeDoc {
  name: string;
  help: string;
  displays: string[];
  fields: FieldDoc[];
  /** Which `media_store.py` kind(s) this type's `file =` field browses —
   *  `[]` for a type with no `file =` at all. Rides on the scene type rather
   *  than the field because the same field means videos on `video` and .sid
   *  files on `waveform`. */
  media_kinds: string[];
}

export interface ParamDoc {
  name: string;
  type: string;
  default: unknown;
  required: boolean;
  help: string;
}

export interface OverlayDoc {
  name: string;
  help: string;
  params: ParamDoc[];
}

/** `GET /api/introspect`. `modes` and `live_targets` arrive too and stay
 *  untyped here: the Tune panel reads the *resolved* knobs off the state feed
 *  (`LiveKnob`), which is the same registry already filtered to what the
 *  running scene has, so a screen never needs the unfiltered catalog. */
export interface Introspection {
  sections: SectionDoc[];
  scene_types: SceneTypeDoc[];
  overlays: OverlayDoc[];
  /** The sixteen C64 colors, in the host's *live* table — a host that has
   *  matched the machine's own palette offers the colors it really emits. */
  palette: Swatch[];
}

// -- one config file: what it *does* contain --------------------------------

/** A field's loaded value. `is_default` is the same comparison
 *  `config_serialize` makes when deciding whether a field is worth writing,
 *  which is what powers "only show what I've changed". */
export interface FormField {
  name: string;
  value: unknown;
  /** What the field falls back to when this file says nothing — the machine
   *  settings layer, not the dataclass default `FieldDoc.default` carries. It
   *  is what a `reset` edit leaves behind, so a form can show that before
   *  asking for one. */
  baseline: unknown;
  is_default: boolean;
}

export interface FormSection {
  name: string;
  fields: FormField[];
}

export interface FormScene {
  type: string;
  name: string;
  fields: FormField[];
  overlays: unknown[];
}

export interface ConfigForm {
  sections: FormSection[];
  scenes: FormScene[];
}

/** A key the loader did not recognize — a typo, or a field from a newer
 *  version. `hint` carries the loader's own suggestion when it has one, and is
 *  null when all it can say is that the key is unknown. */
export interface UnknownKey {
  section: string;
  key: string;
  hint: string | null;
}

/** `GET /api/configs/{ref}`. A file that will not parse still comes back with
 *  its `text` and an `error`; `form` is null for that and for an ensemble
 *  master, which `config_serialize` refuses to describe by design. */
export interface ConfigDetail {
  path: string;
  abs_path: string;
  text: string;
  size: number;
  mtime: number;
  kind: "config" | "ensemble";
  systems: string[];
  unknown_keys: UnknownKey[];
  error: string | null;
  form: ConfigForm | null;
}

/** A setting that came from a layer *under* the file being edited — the
 *  machine settings. Present on a failed report only, and only when the failure
 *  named a key this file does not set, which is the case where the error would
 *  otherwise send the reader hunting through the wrong file. `error` is set
 *  instead of `key` when the settings file itself will not parse. */
export interface LayerNote {
  path: string;
  section: string;
  key: string;
  value?: unknown;
  error: string | null;
}

/** One problem `doctor.validate_load_result` found — the collect-all offline
 *  check `--doctor --skip-probe` runs, reachable here as the `diagnostics` on
 *  a pre-flight report so the console can say everything wrong with a config
 *  at once rather than one thing per click. */
export interface Diagnostic {
  level: "ok" | "warn" | "error";
  category: string;
  subject: string;
  message: string;
  hint: string | null;
}

/** `POST /api/configs/{ref}/validate`, and the body of the 422 a refused save
 *  answers with. `diagnostics` is only ever populated by a pre-flight check
 *  (an absent `text`, meaning "the file on disk") — a check of unsaved text
 *  has no environment to run the collect-all pass against. */
export interface ValidationReport {
  ok: boolean;
  error: string | null;
  messages: string[];
  unknown_keys: UnknownKey[];
  systems: string[];
  layers: LayerNote[];
  warnings: Warning[];
  diagnostics: Diagnostic[];
}

/** Something that loads but will bite — media a scene names that isn't on this
 *  host. Never a refusal: a path may be filled in before showtime, or name a
 *  file on another machine in an ensemble. `scene` and `field` are null on a
 *  warning about the show rather than about one place in it, which is what a
 *  scene added before its media is. */
export interface Warning {
  system: string;
  scene: number | null;
  field: string | null;
  detail: string;
}

/** `PUT /api/configs/{ref}`. `backup` names the dotfile sibling holding what
 *  was replaced, or is null when there was nothing there to keep. */
export interface ConfigWritten {
  ok: boolean;
  path: string;
  abs_path: string;
  bytes: number;
  backup: string | null;
  unknown_keys: UnknownKey[];
  systems: string[];
  warnings: Warning[];
}

/** One field edit for `PATCH /api/configs/{ref}`. Names a `section` or a
 *  `scene` by index — never both — and carries either a `value` or `reset`,
 *  which puts the field back to what the layer underneath the file says. The
 *  server composes the TOML from these, so nothing here writes TOML. */
export interface ConfigEdit {
  section?: string;
  scene?: number;
  field: string;
  value?: unknown;
  reset?: boolean;
}

/** `PATCH /api/configs/{ref}` — a write, plus what the server made of the
 *  edits and the text it composed from them. */
export interface ConfigPatched extends ConfigWritten {
  edits: ConfigEdit[];
  text: string;
}

/** A scene added or removed — a structural change to the file rather than a
 *  new value for a field, so it reports which index moved rather than a list
 *  of edits. `scene.added` is where the new one landed. */
export interface SceneChanged extends ConfigWritten {
  scene: {
    added?: number;
    removed?: number;
    type: string;
    name?: string | null;
    copied_from?: number | null;
  };
  text: string;
}

// -- the performance surface: what the running show is doing right now ------

/** One system's beat grid, from `perf_console._tempo_dict`. The two phases are
 *  sampled against a single instant so a client can extrapolate from them. */
export interface TempoState {
  bpm: number;
  running: boolean;
  source: string;
  beats_per_bar: number;
  beat_phase: number;
  bar_phase: number;
}

/** One configured clip slot, with the state the bridge stamps on it. */
export interface Clip {
  slot: number;
  name: string;
  type: string | null;
  pad: number | null;
  pad_type: string;
  launch: string;
  quantize: string;
  loop: boolean;
  state: "active" | "armed" | "loaded";
}

/** The clip waiting on a quantize boundary. `beats_remaining` is null when the
 *  clock is stopped — nothing to count in, the launch is immediate. */
export interface ArmedClip {
  slot: number;
  quantize: string;
  beats_remaining: number | null;
}

/** What a slider needs: a named number, its declared range, and where in that
 *  range it currently sits. Both live-tune surfaces produce it — the effect
 *  rack from a layer's `LIVE_PARAMS`, the tune panel from a scalar `LiveKnob` —
 *  so they share one control. */
export interface Knob {
  name: string;
  value: number;
  min: number;
  max: number;
  norm: number;
}

/** One declared `LIVE_PARAMS` field of an effect layer. */
export type FxParam = Knob;

export interface FxLayer {
  index: number;
  name: string;
  enabled: boolean;
  mod_source: string;
  params: FxParam[];
}

/** One live-tune knob the *current scene* actually has, from
 *  `perf_console._live_dict`. The host tries every target
 *  `introspect.live_targets()` declares against the running scene and sends
 *  only the ones that resolve, so a rendered control always writes somewhere.
 *  `min`/`max`/`norm` come with a scalar, `choices` with a choice.
 *  `vocabulary` mirrors `FieldDoc.vocabulary` — `"c64color"` (border/
 *  background, Live DJ/VJ Phase 7) swaps the `<select>` for swatches. */
export interface LiveKnob {
  target: string;
  group: string;
  name: string;
  kind: "scalar" | "choice";
  value: number | string | null;
  min?: number;
  max?: number;
  norm?: number;
  choices?: string[];
  vocabulary?: string;
}

/** The A/B loop machine's state (Live DJ/VJ Phase 7), content-seconds like
 *  `TransportState.position`. `"none"` before a mark, `"armed"` after A,
 *  `"active"` once B closes it and it starts looping. */
export interface LoopState {
  state: "none" | "armed" | "active";
  a: number | null;
  b: number | null;
}

/** The current scene's DJ transport surface, from `perf_console._transport_dict`.
 *  Null on `PerfSystem` for a scene that declares none (a generator, a
 *  picture, a scope) — the console renders no transport bar rather than one
 *  that writes nowhere. `duration` is null for a scene with no fixed length. */
export interface TransportState {
  position: number;
  duration: number | null;
  frozen: boolean;
  loop: LoopState;
  /** Pad numbers holding a saved loop preset for the current video. */
  loop_slots: number[];
}

/** One knob change the running show is holding: where it started, where it is
 *  now, and where a save-back would write it. `field` is the config field's own
 *  name and `scene` says which part of the file carries it — null for `[color]`,
 *  otherwise the index of the `[[scenes]]` block. `field` is null when nothing
 *  carries it at all; such a change is still listed, because one that will be
 *  lost when the show ends is exactly the one worth saying so about. `key`
 *  identifies the row to the host, and is not the target: the same per-scene
 *  knob turned during two scenes is two rows. */
export interface TuneChange {
  key: string;
  target: string;
  old: number | string | null;
  new: number | string | null;
  field: string | null;
  scene: number | null;
}

/** `LiveKnob[]` is what can be turned; this is what *has* been, from
 *  `perf_console._tuned_dict`. A one-shot run offers these back at exit, on a
 *  terminal a daemon does not have — so the browser is where the offer is made
 *  instead. `snippet` replaces it for a run with no config file to write to. */
export interface TunedState {
  changes: TuneChange[];
  savable: number;
  config_path: string;
  snippet?: string;
}

/** `POST /api/session/live-tune`. The store's own patch report, plus which
 *  targets went into the file and which were left in the record because no
 *  config field carries them. */
export interface LiveTuneSaved extends ConfigPatched {
  saved: string[];
  kept_out: string[];
}

/** One scene in the running playlist, for a console that offers a jump.
 *  `duration_s` is null for a scene that runs until its source ends. */
export interface SceneRow {
  index: number;
  name: string;
  duration_s: number | null;
  is_current: boolean;
}

/** One system's whole performance state — `perf_console._system_state`. */
export interface PerfSystem {
  name: string;
  current_scene: string | null;
  scene_index: number;
  paused: boolean;
  scenes: SceneRow[];
  tempo: TempoState;
  active_slot: number | null;
  armed: ArmedClip | null;
  clips: Clip[];
  effects: FxLayer[];
  live: LiveKnob[];
  tuned: TunedState;
  /** Slots that hold a saved look, so a recall pad lights only when there is
   *  something to recall. */
  looks: number[];
  /** The current scene's DJ transport (freeze/scrub/rw/ff/A-B loop), or null
   *  for a scene that has none. */
  transport: TransportState | null;
}

/** A frame off `/api/ws`: the performance console's payload with the
 *  supervisor bolted on. Only the keys this app reads are named; the rest
 *  arrive untouched for the screens that will. */
export interface StateFrame {
  role?: Role;
  session?: SessionStatus;
  log?: LogLine[];
  multi?: boolean;
  systems?: PerfSystem[];
  [key: string]: unknown;
}
