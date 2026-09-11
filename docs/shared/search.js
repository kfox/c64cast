// Client-side search: no server, no build-time framework, just the JSON
// index scripts/build_site.py writes next to this file. Every page loads it
// and fetches the index relative to itself, so the same script works at any
// depth (`index.html`, `guide/04-setting-up.html`, ...).
//
// The ranking/highlighting core above is plain data-in-data-out logic with no
// DOM dependency; the wiring below it is the only part that touches
// `document`. That split is what lets tests/test_search_js.mjs `require()`
// this same file under Node (via the `module.exports` guard at the bottom)
// without a browser or a build step -- `typeof document` is the one signal
// that tells the two environments apart.
(function () {
  "use strict";

  const TITLE_WEIGHT = 100; // a title hit always outranks any text hit
  const TEXT_WEIGHT_MAX = 40; // a text hit at position 0
  const TEXT_WEIGHT_DECAY = 20; // chars per point of falloff after that
  const TEXT_WEIGHT_FLOOR = 1; // a text hit is still worth more than no hit

  function words(query) {
    return query.toLowerCase().split(/\s+/).filter(Boolean);
  }

  // Every query word must appear somewhere (title or text) -- a query is a
  // refinement, not a bag of optional hints. Title hits outrank text hits,
  // and an earlier text hit outranks a later one (more likely the lede).
  function score(entry, terms) {
    let total = 0;
    for (const term of terms) {
      const inTitle = entry.titleLower.includes(term);
      const at = entry.textLower.indexOf(term);
      if (!inTitle && at < 0) return -1;
      total +=
        (inTitle ? TITLE_WEIGHT : 0) +
        (at >= 0 ? Math.max(TEXT_WEIGHT_MAX - at / TEXT_WEIGHT_DECAY, TEXT_WEIGHT_FLOOR) : 0);
    }
    return total;
  }

  function snippet(entry, terms) {
    const text = entry.text;
    let at = -1;
    for (const term of terms) {
      const found = entry.textLower.indexOf(term);
      if (found >= 0 && (at < 0 || found < at)) at = found;
    }
    if (at < 0) return text.slice(0, 140);
    const start = Math.max(0, at - 60);
    const prefix = start > 0 ? "…" : "";
    return prefix + text.slice(start, start + 160);
  }

  function escapeHtml(text) {
    return text.replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  // Finds every term's match ranges against the *plain* text first, merges
  // the overlapping ones, then escapes and wraps in a single left-to-right
  // pass -- doing it a term at a time against the growing marked-up string
  // (the obvious way) lets a later term's regex match literal characters an
  // earlier one just inserted (e.g. the "ark" in a `<mark>` tag it added).
  function mark(text, terms) {
    const lower = text.toLowerCase();
    const ranges = [];
    for (const term of terms) {
      if (!term) continue;
      let from = 0;
      let at;
      while ((at = lower.indexOf(term, from)) !== -1) {
        ranges.push([at, at + term.length]);
        from = at + term.length;
      }
    }
    ranges.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const range of ranges) {
      const last = merged[merged.length - 1];
      if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
      else merged.push(range);
    }
    let out = "";
    let pos = 0;
    for (const [start, end] of merged) {
      out += escapeHtml(text.slice(pos, start));
      out += "<mark>" + escapeHtml(text.slice(start, end)) + "</mark>";
      pos = end;
    }
    return out + escapeHtml(text.slice(pos));
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { words, score, snippet, mark, escapeHtml };
  }

  if (typeof document === "undefined") return;

  const input = document.getElementById("site-search");
  const results = document.getElementById("search-results");
  if (!input || !results) return;

  const indexUrl = new URL(input.dataset.index, document.baseURI);
  // Every entry's `url` is site-root-relative (e.g. "guide/04-x.html"); the
  // index file itself lives at the site root, so its own directory is that
  // root, whatever depth the current page is at.
  const siteRoot = new URL(".", indexUrl);

  let entries = null;
  let pending = null;
  let active = -1;
  // Bumped on every search() call and captured in its closure, so a fetch
  // that resolves after a later (or emptied) query no longer wins the race
  // and reopens the dropdown with an answer to a question nobody is asking.
  let searchToken = 0;

  function load() {
    if (entries) return Promise.resolve(entries);
    if (pending) return pending;
    pending = fetch(indexUrl)
      .then((r) => r.json())
      .then((data) => {
        // Lowercased once here rather than by score() on every keystroke --
        // the index is fetched once and never mutated, so every later
        // search would otherwise re-lowercase the whole corpus per term.
        entries = data.map((e) => ({
          ...e,
          titleLower: e.title.toLowerCase(),
          textLower: e.text.toLowerCase(),
        }));
        pending = null;
        return entries;
      })
      .catch((err) => {
        pending = null;
        throw err;
      });
    return pending;
  }

  // The only thing that makes the dropdown's result set stale: hides it,
  // drops the markup `querySelectorAll` would otherwise still find (`hidden`
  // does not remove elements from the DOM), and clears the keyboard-nav
  // pointer into it. Escape and an outside click both dismiss through here,
  // so neither leaves a dismissed result reachable by a bare Enter afterward.
  function closeResults() {
    results.hidden = true;
    results.innerHTML = "";
    active = -1;
  }

  function render(matches, terms) {
    active = -1;
    if (matches.length === 0) {
      results.innerHTML = '<li class="search-empty">No matches</li>';
      results.hidden = false;
      return;
    }
    results.innerHTML = matches
      .slice(0, 20)
      .map((entry) => {
        const href = new URL(entry.url, siteRoot).href;
        return (
          "<li><a href=\"" +
          href +
          "\">" +
          "<span class=\"result-title\">" +
          mark(entry.title, terms) +
          "</span>" +
          "<span class=\"path\">" +
          escapeHtml(entry.url) +
          "</span>" +
          "<span class=\"snippet\">" +
          mark(snippet(entry, terms), terms) +
          "</span>" +
          "</a></li>"
        );
      })
      .join("");
    results.hidden = false;
  }

  function search(query) {
    const token = ++searchToken;
    const terms = words(query);
    if (terms.length === 0) {
      closeResults();
      return;
    }
    load()
      .then((data) => {
        if (token !== searchToken) return;
        const matches = data
          .map((entry) => ({ entry, s: score(entry, terms) }))
          .filter((m) => m.s >= 0)
          .sort((a, b) => b.s - a.s)
          .map((m) => m.entry);
        render(matches, terms);
      })
      .catch(() => {
        if (token !== searchToken) return;
        results.innerHTML = '<li class="search-empty">Search is unavailable right now</li>';
        results.hidden = false;
      });
  }

  input.addEventListener("focus", load);
  input.addEventListener("input", () => search(input.value));

  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeResults();
      return;
    }
    const items = results.querySelectorAll("li a");
    if (event.key === "ArrowDown" && items.length) {
      event.preventDefault();
      active = Math.min(active + 1, items.length - 1);
    } else if (event.key === "ArrowUp" && items.length) {
      event.preventDefault();
      active = Math.max(active - 1, 0);
    } else if (event.key === "Enter") {
      // No arrow key yet on this query is the common case, not an edge
      // case -- Enter goes to the top-ranked result then, same as active
      // being explicitly on it.
      const target = active >= 0 ? items[active] : items[0];
      if (!target) return;
      event.preventDefault();
      window.location.href = target.href;
      return;
    } else {
      return;
    }
    items.forEach((a, i) => a.parentElement.classList.toggle("active", i === active));
    items[active].scrollIntoView({ block: "nearest" });
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".search")) closeResults();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.target === input) return;
    const tag = event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || event.target.isContentEditable) return;
    event.preventDefault();
    input.focus();
  });
})();
