// Client-side search over the JSON index scripts/build_site.py writes next to
// this file, found through each page's own `data-index` path so the same file
// works at any page depth. The ranking core below is DOM-free and exported for search.test.mjs;
// the wiring past the `typeof document` guard is the only part touching the DOM.
(function () {
  "use strict";

  const TITLE_WEIGHT = 100;
  const TEXT_WEIGHT_MAX = 40;
  const TEXT_WEIGHT_DECAY = 20; // chars per point of falloff
  const TEXT_WEIGHT_FLOOR = 1;

  function words(query) {
    return query.toLowerCase().split(/\s+/).filter(Boolean);
  }

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

  // Ranges are found against the *plain* text and merged before anything is
  // escaped or wrapped: marking one term at a time against the growing marked-up
  // string lets a later term match characters inside a `<mark>` an earlier one
  // inserted (the "ark" in "market").
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
  // Entry urls are site-root-relative, and the index file sits at the site root.
  const siteRoot = new URL(".", indexUrl);

  let entries = null;
  let pending = null;
  let active = -1;
  // Captured by each search() call, so a fetch resolving after a later query
  // cannot render an answer to a question nobody is asking.
  let searchToken = 0;

  function load() {
    if (entries) return Promise.resolve(entries);
    if (pending) return pending;
    pending = fetch(indexUrl)
      .then((r) => r.json())
      .then((data) => {
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

  // `hidden` leaves the items in the DOM for `querySelectorAll`, so the markup
  // goes too; the token bump stops a fetch already in flight from reopening
  // what was just dismissed.
  function closeResults() {
    results.hidden = true;
    results.innerHTML = "";
    active = -1;
    searchToken++;
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
          escapeHtml(href) +
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

  input.addEventListener("focus", () => {
    load().catch(() => {});
  });
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
