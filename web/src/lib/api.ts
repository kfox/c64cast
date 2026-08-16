import type {
  ConfigDetail,
  ConfigEdit,
  ConfigIndex,
  ConfigPatched,
  ConfigWritten,
  Introspection,
  LogLine,
  SessionStatus,
  ValidationReport,
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

  /** `start`, `switch` and `stop` all answer 202: the supervisor has claimed
   *  the transition, not finished it. What actually happened arrives on the
   *  state feed, which is why nothing here waits for a result. */
  start: (config: string | null) => request<unknown>("POST", "/api/session/start", { config }),
  switch: (config: string | null) => request<unknown>("POST", "/api/session/switch", { config }),
  stop: () => request<unknown>("POST", "/api/session/stop"),
  reload: () => request<unknown>("POST", "/api/session/reload"),
};
