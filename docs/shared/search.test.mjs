// Tests for search.js, run as `node --test docs/shared/search.test.mjs`.
// search.js guards its DOM-wiring half behind `typeof document === "undefined"`,
// so require()'ing it with no `document` global reaches only the ranking core.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { words, score, snippet, mark, escapeHtml } = require("./search.js");

function entry(title, text) {
  return { title, text, titleLower: title.toLowerCase(), textLower: text.toLowerCase() };
}

test("words: splits, lowercases, drops empty tokens", () => {
  assert.deepEqual(words("  Bus  Operation "), ["bus", "operation"]);
  assert.deepEqual(words(""), []);
});

test("score: every term must match somewhere, title or text", () => {
  const e = entry("Quick Start", "turn on the DMA service");
  assert.ok(score(e, ["quick"]) > 0);
  assert.equal(score(e, ["quick", "nope"]), -1);
});

test("score: a title hit outranks a text-only hit", () => {
  const titleHit = entry("DMA Service", "unrelated body");
  const textOnly = entry("Something Else", "the dma service is mentioned here");
  assert.ok(score(titleHit, ["dma"]) > score(textOnly, ["dma"]));
});

test("score: an earlier text hit outranks a later one", () => {
  const early = entry("x", "dma right at the start of the page");
  const late = entry("x", "a very long lede before we ever mention dma at all");
  assert.ok(score(early, ["dma"]) > score(late, ["dma"]));
});

test("snippet: falls back to the start of the text with no match", () => {
  const e = entry("x", "y".repeat(200));
  assert.equal(snippet(e, ["nope"]), "y".repeat(140));
});

test("snippet: centers on the earliest term match", () => {
  const e = entry("x", "a".repeat(100) + "TARGET" + "b".repeat(100));
  const s = snippet(e, ["target"]);
  assert.ok(s.includes("TARGET"));
  assert.ok(s.startsWith("…"));
});

test("escapeHtml: escapes the five HTML-significant characters", () => {
  assert.equal(escapeHtml(`<a href="x">&'</a>`), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;");
});

test("mark: highlights a single match without corrupting the string", () => {
  assert.equal(mark("the market is open", ["market"]), "the <mark>market</mark> is open");
});

test("mark: a later term that is a substring of an earlier match's word does not corrupt the markup", () => {
  // The shipped bug: each term's regex was re-run against the growing marked-up
  // string, so "ark" matched inside the <mark> tag "market" had just inserted.
  assert.equal(mark("the market is open", ["market", "ark"]), "the <mark>market</mark> is open");
});

test("mark: escapes HTML-significant characters outside the highlighted span", () => {
  assert.equal(mark("a <b> & c", ["b"]), "a &lt;<mark>b</mark>&gt; &amp; c");
});

test("mark: a query containing regex-special characters is treated as literal text", () => {
  assert.equal(mark("a (b) c", ["(b)"]), "a <mark>(b)</mark> c");
});

test("mark: two non-adjacent matches each get their own span", () => {
  assert.equal(
    mark("overlap overlapping", ["overlap", "lapping"]),
    "<mark>overlap</mark> <mark>overlapping</mark>",
  );
});

// A fake DOM for search.js's wiring: an element that records its own listeners
// so a test can fire them, and a `fetch` a test decides when to resolve.
function fakeElement() {
  const listeners = {};
  return {
    dataset: { index: "search-index.json" },
    hidden: true,
    innerHTML: "",
    value: "",
    addEventListener(type, fn) {
      (listeners[type] ??= []).push(fn);
    },
    fire(type, overrides = {}) {
      for (const fn of listeners[type] || []) fn({ preventDefault() {}, target: this, ...overrides });
    },
    querySelectorAll: () => [],
    closest: () => null,
  };
}

function flush() {
  // Two turns: one for fetch()'s own promise, one for the .then(r => r.json())
  // between it and search()'s .then() that reads searchToken.
  return new Promise((resolve) => setTimeout(resolve, 0)).then(
    () => new Promise((resolve) => setTimeout(resolve, 0)),
  );
}

function loadWiredSearch() {
  const require = createRequire(import.meta.url);
  const resolved = require.resolve("./search.js");
  // The tests above already required search.js with no `document` global, and
  // CommonJS caches by resolved path, so the wired half needs the entry cleared.
  delete require.cache[resolved];

  const input = fakeElement();
  const results = fakeElement();
  globalThis.document = {
    baseURI: "https://example.invalid/guide/x.html",
    getElementById: (id) => (id === "site-search" ? input : id === "search-results" ? results : null),
    addEventListener() {},
  };

  let deliver = null;
  globalThis.fetch = () =>
    new Promise((resolve) => {
      deliver = (data) => resolve({ json: () => Promise.resolve(data) });
    });

  require("./search.js");
  return {
    input,
    results,
    type(query) {
      input.value = query;
      input.fire("input");
    },
    escape() {
      input.fire("keydown", { key: "Escape" });
    },
    deliverIndex(data) {
      deliver(data);
    },
    cleanup() {
      delete globalThis.document;
      delete globalThis.fetch;
    },
  };
}

test("search(): a fetch that resolves after the query was cleared does not reopen the dropdown", async () => {
  const page = loadWiredSearch();
  try {
    page.type("bus");
    page.type("");
    page.deliverIndex([{ url: "guide/x.html", title: "Bus", text: "bus service info" }]);
    await flush();
    assert.equal(page.results.hidden, true);
    assert.equal(page.results.innerHTML, "");
  } finally {
    page.cleanup();
  }
});

test("search(): the latest query's results still render once the index resolves", async () => {
  const page = loadWiredSearch();
  try {
    page.type("bus");
    page.deliverIndex([{ url: "guide/x.html", title: "Bus", text: "bus service info" }]);
    await flush();
    assert.equal(page.results.hidden, false);
    assert.match(page.results.innerHTML, /Bus/);
  } finally {
    page.cleanup();
  }
});

test("search(): a fetch still in flight when Escape dismisses it does not reopen the dropdown once it resolves", async () => {
  const page = loadWiredSearch();
  try {
    page.type("bus");
    page.escape();
    page.deliverIndex([{ url: "guide/x.html", title: "Bus", text: "bus service info" }]);
    await flush();
    assert.equal(page.results.hidden, true);
    assert.equal(page.results.innerHTML, "");
  } finally {
    page.cleanup();
  }
});
