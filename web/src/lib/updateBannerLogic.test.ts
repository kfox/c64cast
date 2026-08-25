import { describe, expect, it } from "vitest";

import type { UpdateState } from "./types";
import {
  browserStore,
  DISMISSED_KEY,
  isDismissed,
  isPending,
  isStale,
  isStaleDismissed,
  lastAnsweredAt,
  readDismissed,
  writeDismissed,
  type KeyValueStore,
} from "./updateBannerLogic";

const DAY_S = 24 * 60 * 60;
const NOW = 1_800_000_000_000;
/** What the host sends as `stale_after_days` — `update_state.py` owns the
 *  real number; nothing on this side hard-codes it. */
const STALE_AFTER = 30;

/** Epoch *seconds* `days` before NOW — the API reports seconds, the browser
 *  counts milliseconds, and mixing the two is the whole bug this guards. */
function daysAgo(days: number): number {
  return NOW / 1000 - days * DAY_S;
}

/** A plain-object stand-in for `localStorage` — this project's vitest
 *  environment is `node`, which has no DOM `Storage` global. */
function fakeStorage(initial: Record<string, string> = {}): KeyValueStore {
  const data = { ...initial };
  return {
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => {
      data[key] = value;
    },
  };
}

function state(over: Partial<UpdateState>): UpdateState {
  return { checked: true, running_version: "0.5.0", stale_after_days: STALE_AFTER, ...over };
}

describe("isPending", () => {
  it("is false with no state yet", () => {
    expect(isPending(null)).toBe(false);
  });

  it("is false when no check has ever completed", () => {
    expect(isPending({ checked: false, running_version: "0.5.0" })).toBe(false);
  });

  it("is false when the last check couldn't compare", () => {
    expect(isPending(state({ newer: null, latest_version: "0.6.0" }))).toBe(false);
  });

  it("is false when already up to date", () => {
    expect(isPending(state({ newer: false, latest_version: "0.5.0" }))).toBe(false);
  });

  it("is true for a newer release with a version to show", () => {
    expect(isPending(state({ newer: true, latest_version: "0.6.0" }))).toBe(true);
  });

  it("is false for newer=true with no version named", () => {
    expect(isPending(state({ newer: true, latest_version: null }))).toBe(false);
  });
});

describe("isDismissed", () => {
  const pending = state({ newer: true, latest_version: "0.6.0" });

  it("is false with no recorded dismissal", () => {
    expect(isDismissed(pending, null)).toBe(false);
  });

  it("is true once the pending version has been dismissed", () => {
    expect(isDismissed(pending, "0.6.0")).toBe(true);
  });

  it("is false for a dismissal of an older version — a newer release re-shows the banner", () => {
    expect(isDismissed(pending, "0.5.5")).toBe(false);
  });

  it("is false with no state at all", () => {
    expect(isDismissed(null, "0.6.0")).toBe(false);
  });
});

describe("lastAnsweredAt / isStale", () => {
  it("dates a silent host from when the silence began, not its last attempt", () => {
    // The appliance case: a daily timer keeps failing and keeps bumping
    // checked_at, while the answer it holds gets older and older.
    const silent = state({
      checked_at: daysAgo(0),
      unanswered_since: daysAgo(60),
      latest_version: "0.6.0",
      newer: false,
    });
    expect(lastAnsweredAt(silent)).toBe(NOW - 60 * DAY_S * 1000);
    expect(isStale(silent, NOW)).toBe(true);
  });

  it("dates an answering host from its last attempt", () => {
    const answering = state({ checked_at: daysAgo(2), latest_version: "0.6.0", newer: false });
    expect(lastAnsweredAt(answering)).toBe(NOW - 2 * DAY_S * 1000);
    expect(isStale(answering, NOW)).toBe(false);
  });

  it(`is false until the host's ${STALE_AFTER} days have passed`, () => {
    const almost = state({ checked_at: daysAgo(0), unanswered_since: daysAgo(STALE_AFTER - 1) });
    expect(isStale(almost, NOW)).toBe(false);
  });

  it("follows the host's threshold rather than one of its own", () => {
    const silent = state({ checked_at: daysAgo(0), unanswered_since: daysAgo(45) });
    expect(isStale({ ...silent, stale_after_days: 90 }, NOW)).toBe(false);
    expect(isStale({ ...silent, stale_after_days: 7 }, NOW)).toBe(true);
  });

  it("is false when the host names no threshold at all", () => {
    const silent = state({ checked_at: daysAgo(0), unanswered_since: daysAgo(365) });
    expect(isStale({ ...silent, stale_after_days: undefined }, NOW)).toBe(false);
  });

  it("is false with no state yet, and when no check has ever run", () => {
    expect(isStale(null, NOW)).toBe(false);
    expect(isStale({ checked: false, running_version: "0.5.0" }, NOW)).toBe(false);
  });

  it("is false for a host that has never checked in — nothing says how long", () => {
    expect(isStale(state({}), NOW)).toBe(false);
  });

  it("is true for a box that has never once reached PyPI, dated from its first try", () => {
    const neverAnswered = state({
      checked_at: daysAgo(0),
      unanswered_since: daysAgo(45),
      latest_version: null,
      newer: null,
    });
    expect(isStale(neverAnswered, NOW)).toBe(true);
  });
});

describe("isStaleDismissed", () => {
  const silent = state({ running_version: "0.5.0" });

  it("is false with no recorded dismissal", () => {
    expect(isStaleDismissed(silent, null)).toBe(false);
  });

  it("is true for the release it was dismissed on — until the next upgrade", () => {
    expect(isStaleDismissed(silent, "0.5.0")).toBe(true);
  });

  it("is false again once this install has been upgraded", () => {
    expect(isStaleDismissed(silent, "0.4.0")).toBe(false);
  });
});

describe("readDismissed / writeDismissed", () => {
  it("round-trips through the store", () => {
    const store = fakeStorage();
    writeDismissed(store, DISMISSED_KEY, "0.6.0");
    expect(readDismissed(store, DISMISSED_KEY)).toBe("0.6.0");
  });

  it("reads null when nothing has been written", () => {
    expect(readDismissed(fakeStorage(), DISMISSED_KEY)).toBeNull();
  });

  it("keeps the two banners' dismissals apart", () => {
    const store = fakeStorage();
    writeDismissed(store, DISMISSED_KEY, "0.6.0");
    expect(readDismissed(store, "c64cast.stale-dismissed-version")).toBeNull();
  });

  it("uses one fixed key per banner, not one per version", () => {
    const store = fakeStorage();
    writeDismissed(store, DISMISSED_KEY, "0.6.0");
    writeDismissed(store, DISMISSED_KEY, "0.7.0");
    expect(store.getItem(DISMISSED_KEY)).toBe("0.7.0");
  });

  it("a read that throws is treated as no dismissal", () => {
    const store: KeyValueStore = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {},
    };
    expect(readDismissed(store, DISMISSED_KEY)).toBeNull();
  });

  it("no store at all is treated as no dismissal, and a write to it is a no-op", () => {
    expect(readDismissed(null, DISMISSED_KEY)).toBeNull();
    expect(() => writeDismissed(null, DISMISSED_KEY, "0.6.0")).not.toThrow();
  });

  it("a write that throws does not raise", () => {
    const store: KeyValueStore = {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota exceeded");
      },
    };
    expect(() => writeDismissed(store, DISMISSED_KEY, "0.6.0")).not.toThrow();
  });
});

describe("browserStore", () => {
  it("is null where reading the global throws — no window here, a blocked browser there", () => {
    expect(browserStore()).toBeNull();
  });
});
