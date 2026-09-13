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
  UpdateState,
  ValidationReport,
  ViewerLink,
} from "./types";

/** A non-2xx answer, carrying the status so a caller can tell "you may not"
 *  (403) from "that config is broken" (422) without parsing prose. */
export class ApiError extends Error {
  readonly status: number;
  /** The parsed body, kept whole: a refused config write answers 422 with the
   *  full validation report, not just a message. */
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

/** Parse a response body (JSON if it is JSON, the raw text otherwise) and
 *  throw `ApiError` for anything not 2xx. Shared by `fetch` (via `answer`) and
 *  by `uploadMedia`'s `XMLHttpRequest`, which cannot hand over a `Response`. */
export function settle<T>(status: number, statusText: string, text: string): T {
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (status < 200 || status >= 300) {
    const message = detailOf(parsed, `${status} ${statusText}`);
    throw new ApiError(status, message, parsed);
  }
  return parsed as T;
}

/** `request()`'s response half, built on `settle`. */
async function answer<T>(response: Response): Promise<T> {
  const text = await response.text();
  return settle<T>(response.status, response.statusText, text);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // The token rides in the HttpOnly `SameSite=Strict` cookie the login
  // exchange set; nothing here ever holds it in JS.
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
  update: () => request<UpdateState>("GET", "/api/update"),

  /** Media a `file =` field could name. Memoized per kind by
   *  `introspect.mediaOfKind`, until `forgetMedia` drops it. */
  media: (kind: string, q = "") =>
    request<MediaIndex>("GET", `/api/media?${new URLSearchParams({ kind, q })}`),

  /** Upload a file, streamed straight through as the request body — no JSON
   *  envelope, so this bypasses `request()`. `XMLHttpRequest` because it is
   *  the only browser API that reports request-body progress (`opts.onProgress`);
   *  `opts.signal` wires an `AbortController` to `xhr.abort()`. The kind and
   *  the destination directory are the server's call (it reads the extension
   *  off `name`); a name already taken comes back renamed, never overwritten. */
  uploadMedia: (
    name: string,
    file: File,
    opts: {
      onProgress?: (loaded: number, total: number, computable: boolean) => void;
      signal?: AbortSignal;
    } = {},
  ) =>
    new Promise<MediaUploaded>((resolve, reject) => {
      if (opts.signal?.aborted) {
        reject(new Error("upload canceled"));
        return;
      }
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/media/${encodeURIComponent(name)}`);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (event) => {
        opts.onProgress?.(event.loaded, event.total, event.lengthComputable);
      };
      xhr.onload = () => {
        try {
          resolve(settle<MediaUploaded>(xhr.status, xhr.statusText, xhr.responseText));
        } catch (e) {
          reject(e);
        }
      };
      xhr.onerror = () => reject(new Error("network error"));
      xhr.onabort = () => reject(new Error("upload canceled"));
      opts.signal?.addEventListener("abort", () => xhr.abort());
      xhr.send(file);
    }),

  /** Describes the code, not the run, so it cannot change while the host is
   *  up — `documentation()` in introspect.ts fetches it once. */
  introspect: () => request<Introspection>("GET", "/api/introspect"),

  config: (ref: string) => request<ConfigDetail>("GET", `/api/configs/${refPath(ref)}`),

  /** Load `text` as if it were saved, without saving it — the same check a
   *  save makes. With `text` omitted, checks the file as it stands on disk
   *  instead, which is a start or switch's pre-flight. */
  checkConfig: (ref: string, text?: string) =>
    request<ValidationReport>(
      "POST",
      `/api/configs/${refPath(ref)}/validate`,
      text === undefined ? undefined : { text },
    ),

  /** Refused with 422 if the text does not load — the store never writes a
   *  config that cannot run. */
  saveConfig: (ref: string, text: string) =>
    request<ConfigWritten>("PUT", `/api/configs/${refPath(ref)}`, { text }),

  /** The form's save. `PUT` replaces the file with text this app composed;
   *  `PATCH` names fields and lets the server compose it through the same
   *  dataclasses the loader uses, so the browser never writes TOML. Refused
   *  the same way a `PUT` is: 422 with the whole validation report. */
  patchConfig: (ref: string, edits: ConfigEdit[]) =>
    request<ConfigPatched>("PATCH", `/api/configs/${refPath(ref)}`, { edits }),

  /** Add a scene — a blank one of `type`, or a copy of the scene at `copy`.
   *  Structural rather than a field edit, so it is its own route. Written and
   *  validated immediately, like every other save. */
  addScene: (ref: string, body: { type?: string; copy?: number; after?: number }) =>
    request<SceneChanged>("POST", `/api/configs/${refPath(ref)}/scenes`, body),

  /** Remove the scene at `index`. Refused for the last scene. */
  removeScene: (ref: string, index: number) =>
    request<SceneChanged>("DELETE", `/api/configs/${refPath(ref)}/scenes/${index}`),

  /** Move the scene at `index` to `to`. A no-op move (`index === to`) is
   *  accepted. */
  moveScene: (ref: string, index: number, to: number) =>
    request<SceneChanged>("PATCH", `/api/configs/${refPath(ref)}/scenes/${index}`, { to }),

  /** A new file at `path`: a copy of `copyOf` (any readable ref, including a
   *  packaged example), or a minimal starter when omitted. Refused if `path`
   *  already exists. */
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
   *  held. */
  saveLiveTune: (system: string | null) =>
    request<LiveTuneSaved>("POST", "/api/session/live-tune", { action: "save", system }),

  /** Drop them instead. The show keeps the values it is playing; only the
   *  offer to keep them goes away. */
  discardLiveTune: (system: string | null) =>
    request<{ ok: boolean; discarded: number }>("POST", "/api/session/live-tune", {
      action: "discard",
      system,
    }),

  /** The read-only login link to hand somebody, minting the token on the first
   *  ask. A `POST` even though it reads like a read: the gate lets a viewer
   *  token through every `GET`, and a guest must not be able to fetch the link
   *  that made them one. The host answers with a *path*, since it may be bound
   *  to `0.0.0.0` and cannot know which address this browser used. */
  viewerLink: () => request<ViewerLink>("POST", "/api/viewer-link"),

  /** Which systems can show a picture, and how often the host will encode one.
   *  Asking does not start anything — the stream comes up when an `<img>`
   *  opens `/api/screen/stream`, and goes down when it closes. */
  screen: () => request<ScreenAvailability>("GET", "/api/screen"),

  /** Favorites + recently-launched configs — server-side, so every browser or
   *  phone pointed at this host sees the same list. */
  library: () => request<LibraryState>("GET", "/api/library"),
  favorite: (ref: string, on: boolean) =>
    request<{ favorites: string[] }>("POST", "/api/library/favorites", { ref, on }),
};
