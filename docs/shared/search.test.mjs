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
