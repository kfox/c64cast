import type { ConfigIndex, LogLine, SessionStatus } from "./types";

/** A non-2xx answer, carrying the status so a caller can tell "you may not"
 *  (403) from "that config is broken" (422) without parsing prose. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
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
    throw new ApiError(response.status, detailOf(parsed, `${response.status} ${response.statusText}`));
  }
  return parsed as T;
}

export interface SessionSnapshot extends SessionStatus {
  role?: string | null;
  log?: LogLine[];
}

export const api = {
  session: () => request<SessionSnapshot>("GET", "/api/session"),
  configs: () => request<ConfigIndex>("GET", "/api/configs"),

  /** `start`, `switch` and `stop` all answer 202: the supervisor has claimed
   *  the transition, not finished it. What actually happened arrives on the
   *  state feed, which is why nothing here waits for a result. */
  start: (config: string | null) => request<unknown>("POST", "/api/session/start", { config }),
  switch: (config: string | null) => request<unknown>("POST", "/api/session/switch", { config }),
  stop: () => request<unknown>("POST", "/api/session/stop"),
  reload: () => request<unknown>("POST", "/api/session/reload"),
};
