// Zero-dependency tests for search.js's pure ranking/highlighting core --
// node:test + node:assert, no package.json, no bundler. `search.js` guards
// its DOM-wiring half behind `typeof document === "undefined"`, so
// require()'ing it here (no `document` global under plain Node) exercises
// only the half this file is about.
//
//   node --test docs/shared/
//
// This exists because the mark() bug below (an earlier term's regex
// matching literal characters a previous term had just wrapped in <mark>)
// shipped once and was only caught by a human re-reading the diff -- see
// CHANGELOG.md's "search box" entry and the commit that followed it.

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
  // The regression case: "market" and "ark" both match inside "market", and
  // the buggy version re-ran each term's regex against the growing
  // marked-up string, so "ark" matched literal characters inside the
  // <mark> tag "market" had just inserted.
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

// --- the fetch-race guard (search()'s DOM-wiring half) -----------------
//
// A minimal fake DOM, just enough for search.js's wiring to run: an element
// that records its own event listeners so a test can fire them, and a
// controllable `fetch` so a test can decide exactly when the index "arrives"
// relative to a later keystroke -- the race the real bug (and the real fix)
// is about, not something a unit test of score()/mark() in isolation can
// exercise. Deletes the module from Node's require cache first: the pure-
// function tests above already required search.js with no `document`
// global, and CommonJS caches by resolved path, so a second require() here
// would just return that same cached export unless the cache entry is
// cleared -- this is the one time in the file that's necessary.

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
  // Two hops: one for fetch()'s own promise, one for the .then(r => r.json())
  // in between it and search()'s .then() that reads searchToken. A single
  // microtask turn is not enough to guarantee both have run.
  return new Promise((resolve) => setTimeout(resolve, 0)).then(
    () => new Promise((resolve) => setTimeout(resolve, 0)),
  );
}

function loadWiredSearch() {
  const require = createRequire(import.meta.url);
  const resolved = require.resolve("./search.js");
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
    page.type(""); // cleared before the index ever arrives
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
    page.type("bus"); // starts the fetch; the index has not arrived yet
    page.escape(); // dismissed before it does
    page.deliverIndex([{ url: "guide/x.html", title: "Bus", text: "bus service info" }]);
    await flush();
    assert.equal(page.results.hidden, true);
    assert.equal(page.results.innerHTML, "");
  } finally {
    page.cleanup();
  }
});
