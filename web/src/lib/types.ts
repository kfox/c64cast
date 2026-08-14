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
  name: string;
  size: number;
  mtime: number;
}

export interface ConfigRoot {
  label: string;
  path: string;
}

export interface ConfigIndex {
  roots: ConfigRoot[];
  files: ConfigFile[];
  truncated: boolean;
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
}

export interface SectionDoc {
  name: string;
  help: string;
  fields: FieldDoc[];
}

export interface SceneTypeDoc {
  name: string;
  help: string;
  displays: string[];
  fields: FieldDoc[];
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

/** `GET /api/introspect`. `modes` and `live_targets` arrive too and are left
 *  untyped until a screen reads them — the performance surface will. */
export interface Introspection {
  sections: SectionDoc[];
  scene_types: SceneTypeDoc[];
  overlays: OverlayDoc[];
}

// -- one config file: what it *does* contain --------------------------------

/** A field's loaded value. `is_default` is the same comparison
 *  `config_serialize` makes when deciding whether a field is worth writing,
 *  which is what powers "only show what I've changed". */
export interface FormField {
  name: string;
  value: unknown;
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

/** A key the loader did not recognise — a typo, or a field from a newer
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

/** `POST /api/configs/{ref}/validate`, and the body of the 422 a refused save
 *  answers with. */
export interface ValidationReport {
  ok: boolean;
  error: string | null;
  messages: string[];
  unknown_keys: UnknownKey[];
  systems: string[];
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

/** One declared `LIVE_PARAMS` field. `norm` is the slider position; `value` is
 *  what the layer actually holds. */
export interface FxParam {
  name: string;
  value: number;
  min: number;
  max: number;
  norm: number;
}

export interface FxLayer {
  index: number;
  name: string;
  enabled: boolean;
  mod_source: string;
  params: FxParam[];
}

/** One system's whole performance state — `perf_console._system_state`. */
export interface PerfSystem {
  name: string;
  current_scene: string | null;
  tempo: TempoState;
  active_slot: number | null;
  armed: ArmedClip | null;
  clips: Clip[];
  effects: FxLayer[];
  /** Slots that hold a saved look, so a recall pad lights only when there is
   *  something to recall. */
  looks: number[];
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
