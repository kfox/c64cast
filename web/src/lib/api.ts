import type {
  ConfigDetail,
  ConfigEdit,
  ConfigIndex,
  ConfigPatched,
  ConfigWritten,
  Introspection,
  LibraryState,
  LiveTuneSaved,
  LogLine,
  MediaIndex,
  MediaUploaded,
  SceneChanged,
  ScreenAvailability,
  SessionStatus,
  ValidationReport,
  ViewerLink,
} from "./types";

/** A non-2xx answer, carrying the status so a caller can tell "you may not"
 *  (403) from "that config is broken" (422) without parsing prose. */
export class ApiError extends Error {
  readonly status: number;
  /** The parsed body, kept whole: a refused config write answers 422 with the
   *  full validation report, and a screen that only got the message would have
   *  to ask for it again to show the loader's own diagnostics. */
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** The report inside a 422 from a config write, if that is what this is. */
export function reportOf(e: unknown): ValidationReport | null {
  if (!(e instanceof ApiError) || e.status !== 422) return null;
  const detail = (e.body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && "messages" in detail) {
    return detail as ValidationReport;
  }
  return null;
}

/** FastAPI puts the message in `detail`, which is a string for the errors this
 *  API raises and an object for the config store's validation report. */
function detailOf(body: unknown, fallback: string): string {
  if (typeof body === "string" && body) return body;
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      const error = (detail as { error?: unknown }).error;
      if (typeof error === "string" && error) return error;
    }
  }
  return fallback;
}

/** The response half of a request: parse the body (JSON if it is JSON, the
 *  raw text otherwise) and throw `ApiError` for anything not 2xx. Shared by
 *  `request()`'s JSON calls and `uploadMedia`'s raw-body `PUT`, which can't
 *  go through `request()` itself — that hard-wires `Content-Type:
 *  application/json` and `JSON.stringify`s the body, and a `File` handed to
 *  that would upload the four bytes of the string `"{}"`. */
async function answer<T>(response: Response): Promise<T> {
  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!response.ok) {
    const message = detailOf(parsed, `${response.status} ${response.statusText}`);
    throw new ApiError(response.status, message, parsed);
  }
  return parsed as T;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // `same-origin` credentials, and nothing else: the token rides in the
  // HttpOnly `SameSite=Strict` cookie the login exchange set. The app never
  // holds the token in JS, so an injected script has nothing to steal.
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? { Accept: "application/json" } : {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return answer<T>(response);
}

/** A config ref is `<root-label>/<relative path>`, and its separators are part
 *  of the route (`{ref:path}`), so each segment is escaped on its own rather
 *  than the whole string — a name with a space or a `#` in it still addresses
 *  the file it names. */
function refPath(ref: string): string {
  return ref
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
}

export interface SessionSnapshot extends SessionStatus {
  role?: string | null;
  log?: LogLine[];
}

export const api = {
  session: () => request<SessionSnapshot>("GET", "/api/session"),
  configs: () => request<ConfigIndex>("GET", "/api/configs"),

  /** Media a `file =` field could name — a plain GET, so it can be issued
   *  freely (once per kind a loaded config's scenes actually use, memoized
   *  for the page's lifetime) without a distinct request shape. */
  media: (kind: string, q = "") =>
    request<MediaIndex>("GET", `/api/media?${new URLSearchParams({ kind, q })}`),

  /** Upload a file, streamed straight through as the request body — no JSON
   *  envelope, so this bypasses `request()` and calls `answer()` directly.
   *  The kind and the destination directory are the server's call (it reads
   *  the extension off `name`); a name already taken there comes back
   *  renamed rather than overwritten. */
  uploadMedia: (name: string, file: File) =>
    fetch(`/api/media/${encodeURIComponent(name)}`, {
      method: "PUT",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/octet-stream" },
      body: file,
    }).then((response) => answer<MediaUploaded>(response)),

  /** Describes the code, not the run, so it cannot change while the host is
   *  up — see `introspection()` in introspect.ts, which fetches it once. */
  introspect: () => request<Introspection>("GET", "/api/introspect"),

  config: (ref: string) => request<ConfigDetail>("GET", `/api/configs/${refPath(ref)}`),

  /** Load `text` as if it were saved, without saving it. The same check a save
   *  makes, offered separately so the editor can show the reason before the
   *  file is at stake. */
  checkConfig: (ref: string, text: string) =>
    request<ValidationReport>("POST", `/api/configs/${refPath(ref)}/validate`, { text }),

  /** Refused with 422 if the text does not load — the store never writes a
   *  config that cannot run. */
  saveConfig: (ref: string, text: string) =>
    request<ConfigWritten>("PUT", `/api/configs/${refPath(ref)}`, { text }),

  /** The form's save. `PUT` replaces the file with text this app composed;
   *  `PATCH` names fields and lets the server compose it through the same
   *  dataclasses the loader uses — so the browser never writes TOML, and two
   *  consoles editing different fields don't overwrite each other's sections.
   *  Refused the same way a `PUT` is: 422 with the whole validation report. */
  patchConfig: (ref: string, edits: ConfigEdit[]) =>
    request<ConfigPatched>("PATCH", `/api/configs/${refPath(ref)}`, { edits }),

  /** Add a scene — a blank one of `type`, or a copy of the scene at `copy`.
   *  Structural rather than a field edit, which is why it is its own route:
   *  it changes which scenes exist, not what one of them says. Written and
   *  validated immediately, like every other save. */
  addScene: (ref: string, body: { type?: string; copy?: number; after?: number }) =>
    request<SceneChanged>("POST", `/api/configs/${refPath(ref)}/scenes`, body),

  /** Its pair — a console that could add and not remove would be a one-way
   *  door back to the text editor. Refused for the last scene. */
  removeScene: (ref: string, index: number) =>
    request<SceneChanged>("DELETE", `/api/configs/${refPath(ref)}/scenes/${index}`),

  /** The order of a show, reachable without the text editor: move the scene
   *  at `index` to `to`. A no-op move (`index === to`) is accepted. */
  moveScene: (ref: string, index: number, to: number) =>
    request<SceneChanged>("PATCH", `/api/configs/${refPath(ref)}/scenes/${index}`, { to }),

  /** A new file at `path`: a copy of `copyOf` (any readable ref, including a
   *  packaged example — the onboarding path for one), or a minimal starter
   *  when omitted. Refused if `path` already exists. */
  createConfig: (path: string, copyOf?: string) =>
    request<ConfigWritten>("POST", "/api/configs", { path, copy_of: copyOf }),

  /** Refused for a read-only root or the config the session is currently
   *  running — the store and the route each refuse one of those. */
  deleteConfig: (ref: string) => request<{ ok: boolean }>("DELETE", `/api/configs/${refPath(ref)}`),

  /** `start`, `switch` and `stop` all answer 202: the supervisor has claimed
   *  the transition, not finished it. What actually happened arrives on the
   *  state feed, which is why nothing here waits for a result. */
  start: (config: string | null) => request<unknown>("POST", "/api/session/start", { config }),
  switch: (config: string | null) => request<unknown>("POST", "/api/session/switch", { config }),
  stop: () => request<unknown>("POST", "/api/session/stop"),
  reload: () => request<unknown>("POST", "/api/session/reload"),

  /** Keep the knob changes made since the show started — a `PATCH` of the
   *  running config's `[color]` section under the covers, so it is refused the
   *  same way any other save is, with the file untouched and the changes still
   *  held. A one-shot run asks this at exit; a daemon has no exit to ask at. */
  saveLiveTune: (system: string | null) =>
    request<LiveTuneSaved>("POST", "/api/session/live-tune", { action: "save", system }),

  /** Drop them instead. Nothing on the machine changes — the show keeps the
   *  values it is playing; only the offer to keep them goes away. */
  discardLiveTune: (system: string | null) =>
    request<{ ok: boolean; discarded: number }>("POST", "/api/session/live-tune", {
      action: "discard",
      system,
    }),

  /** The read-only login link to hand somebody, minting the token on the first
   *  ask. A `POST` even though it reads like a read: the gate lets a viewer
   *  token through every `GET`, and a guest must not be able to fetch the link
   *  that made them one. The host answers with a *path* — it may be bound to
   *  `0.0.0.0` and have no idea which of its addresses this browser used. */
  viewerLink: () => request<ViewerLink>("POST", "/api/viewer-link"),

  /** Which systems can show a picture, and how often the host will encode one.
   *  Asking does not start anything — the stream comes up when an `<img>`
   *  opens `/api/screen/stream`, and goes down when it closes. */
  screen: () => request<ScreenAvailability>("GET", "/api/screen"),

  /** Favorites + recently-launched configs — server-side and shared across
   *  every browser or phone pointed at this host, rather than one browser's
   *  `localStorage`. */
  library: () => request<LibraryState>("GET", "/api/library"),
  favorite: (ref: string, on: boolean) =>
    request<{ favorites: string[] }>("POST", "/api/library/favorites", { ref, on }),
};
