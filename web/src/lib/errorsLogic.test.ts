import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { describeError } from "./errorsLogic";

describe("describeError", () => {
  it("prefixes a 403 as not allowed", () => {
    expect(describeError(new ApiError(403, "viewer token"))).toBe("Not allowed: viewer token");
  });

  it("prefixes a 404 as no such configuration", () => {
    expect(describeError(new ApiError(404, "gig.toml"))).toBe(
      "No such configuration: gig.toml",
    );
  });

  it("prefixes a 409 as can't do that right now", () => {
    expect(describeError(new ApiError(409, "host is busy"))).toBe(
      "Can't do that right now: host is busy",
    );
  });

  it("prefixes a 413 as too large to edit", () => {
    expect(describeError(new ApiError(413, "12MB"))).toBe(
      "That file is too large to edit here: 12MB",
    );
  });

  it("prefixes a 422 as will not run", () => {
    expect(describeError(new ApiError(422, "scene 0: no such file"))).toBe(
      "That configuration will not run: scene 0: no such file",
    );
  });

  it("passes through any other status verbatim", () => {
    expect(describeError(new ApiError(500, "internal error"))).toBe("internal error");
  });

  it("falls back to an Error's message", () => {
    expect(describeError(new Error("network down"))).toBe("network down");
  });

  it("stringifies anything that isn't an Error", () => {
    expect(describeError("plain string")).toBe("plain string");
  });
});
