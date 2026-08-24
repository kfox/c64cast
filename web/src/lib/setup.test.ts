import { afterEach, describe, expect, it, vi } from "vitest";

import { probeSetup, submitSetup, waitForRestart } from "./setup";

/** A `fetch` that answers one queued response per call. A queued `Error` is
 *  thrown instead — the closed socket between the host's two apps. */
function fetchQueue(answers: (Response | Error)[]): void {
  const queue = [...answers];
  vi.stubGlobal("fetch", async () => {
    const next = queue.shift();
    if (next === undefined) throw new Error("fetch called more times than the test queued");
    if (next instanceof Error) throw next;
    return next;
  });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const instantly = async (): Promise<void> => {};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("probeSetup", () => {
  it("reports the state a host with setup pending answers", async () => {
    fetchQueue([json({ pending: true, token_settable: true })]);
    expect(await probeSetup()).toEqual({ pending: true, token_settable: true });
  });

  it("reads an ordinary host's 401 as nothing to set up", async () => {
    // The route stops existing once setup completes, so the token gate is what
    // answers — which is exactly the signal that the console should mount.
    fetchQueue([json({ detail: "authentication required" }, 401)]);
    expect(await probeSetup()).toBeNull();
  });

  it("treats an explicit pending:false the same way", async () => {
    fetchQueue([json({ pending: false, token_settable: true })]);
    expect(await probeSetup()).toBeNull();
  });

  it("lets a host that cannot be reached at all throw", async () => {
    fetchQueue([new Error("connection refused")]);
    await expect(probeSetup()).rejects.toThrow("connection refused");
  });
});

describe("submitSetup", () => {
  it("returns the login URL the host hands back", async () => {
    fetchQueue([json({ ok: true, login_url: "/api/login?token=abc&next=%2F" })]);
    expect(await submitSetup({ connection: "u64://10.0.0.5", token: "" })).toBe(
      "/api/login?token=abc&next=%2F",
    );
  });

  it("throws the host's own message for something it refused", async () => {
    fetchQueue([json({ ok: false, error: "a connection target is required" }, 400)]);
    await expect(submitSetup({ connection: "", token: "" })).rejects.toThrow(
      "a connection target is required",
    );
  });

  it("falls back to the status line when there is no message to show", async () => {
    fetchQueue([new Response("", { status: 500, statusText: "Internal Server Error" })]);
    await expect(submitSetup({ connection: "u64://10.0.0.5", token: "" })).rejects.toThrow(
      "500 Internal Server Error",
    );
  });
});

describe("waitForRestart", () => {
  it("keeps asking across the gap where nothing is listening", async () => {
    fetchQueue([
      new Error("connection refused"),
      new Error("connection refused"),
      json({ detail: "authentication required" }, 401),
    ]);
    expect(await waitForRestart(instantly)).toBe(true);
  });

  it("does not mistake the form still being served for a restart", async () => {
    fetchQueue([
      json({ pending: true, token_settable: true }),
      json({ detail: "authentication required" }, 401),
    ]);
    expect(await waitForRestart(instantly)).toBe(true);
  });
});
