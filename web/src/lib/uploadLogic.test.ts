import { describe, expect, it } from "vitest";

import { ApiError, settle } from "./api";
import { percent, uploadLabel } from "./uploadLogic";

describe("percent", () => {
  it("is null when the browser could not measure a total", () => {
    expect(percent(50, 100, false)).toBeNull();
  });

  it("is null when the total is zero", () => {
    expect(percent(0, 0, true)).toBeNull();
  });

  it("rounds to the nearest whole percent", () => {
    expect(percent(1, 3, true)).toBe(33);
  });

  it("never exceeds 100", () => {
    expect(percent(150, 100, true)).toBe(100);
  });
});

describe("uploadLabel", () => {
  it("is indeterminate with an unknown total", () => {
    expect(uploadLabel("clip.mp4", 12, 0, false)).toBe("Uploading clip.mp4…");
  });

  it("shows a percentage and a formatted total at 0%", () => {
    expect(uploadLabel("clip.mp4", 0, 400_000_000, true)).toBe(
      "Uploading clip.mp4 — 0% of 381.5 MB",
    );
  });

  it("shows progress mid-upload", () => {
    expect(uploadLabel("clip.mp4", 200_000_000, 400_000_000, true)).toBe(
      "Uploading clip.mp4 — 50% of 381.5 MB",
    );
  });

  it("shows 100% once complete", () => {
    expect(uploadLabel("clip.mp4", 400_000_000, 400_000_000, true)).toBe(
      "Uploading clip.mp4 — 100% of 381.5 MB",
    );
  });

  it("formats a small file in bytes, with no decimal", () => {
    expect(uploadLabel("tiny.sid", 100, 200, true)).toBe("Uploading tiny.sid — 50% of 200 B");
  });
});

describe("settle", () => {
  it("returns the parsed JSON body for a 2xx status", () => {
    expect(settle<{ ok: boolean }>(200, "OK", '{"ok": true}')).toEqual({ ok: true });
  });

  it("returns null for a 2xx status with an empty body", () => {
    expect(settle(204, "No Content", "")).toBeNull();
  });

  it("throws ApiError with a JSON detail's message", () => {
    expect(() => settle(422, "Unprocessable Entity", '{"detail": "bad scene"}')).toThrow(
      ApiError,
    );
    try {
      settle(422, "Unprocessable Entity", '{"detail": "bad scene"}');
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(422);
      expect((e as ApiError).message).toBe("bad scene");
    }
  });

  it("throws ApiError with a bare string body as the message", () => {
    try {
      settle(500, "Internal Server Error", "database is down");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).message).toBe("database is down");
    }
  });

  it("falls back to the status line for an empty error body", () => {
    try {
      settle(503, "Service Unavailable", "");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).message).toBe("503 Service Unavailable");
    }
  });
});
