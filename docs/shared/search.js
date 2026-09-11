// Client-side search: no server, no build-time framework, just the JSON
// index scripts/build_site.py writes next to this file. Every page loads it
// and fetches the index relative to itself, so the same script works at any
// depth (`index.html`, `guide/04-setting-up.html`, ...).
(function () {
  "use strict";

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

  function load() {
    if (entries || pending) return pending;
    pending = fetch(indexUrl)
      .then((r) => r.json())
      .then((data) => {
        entries = data;
        return data;
      });
    return pending;
  }

  function words(query) {
    return query.toLowerCase().split(/\s+/).filter(Boolean);
  }

  // Every query word must appear somewhere (title or text) -- a query is a
  // refinement, not a bag of optional hints. Title hits outrank text hits,
  // and an earlier text hit outranks a later one (more likely the lede).
  function score(entry, terms) {
    const title = entry.title.toLowerCase();
    const text = entry.text.toLowerCase();
    let total = 0;
    for (const term of terms) {
      const inTitle = title.includes(term);
      const at = text.indexOf(term);
      if (!inTitle && at < 0) return -1;
      total += (inTitle ? 100 : 0) + (at >= 0 ? Math.max(40 - at / 20, 1) : 0);
    }
    return total;
  }

  function snippet(text, terms) {
    const lower = text.toLowerCase();
    let at = -1;
    for (const term of terms) {
      const found = lower.indexOf(term);
      if (found >= 0 && (at < 0 || found < at)) at = found;
    }
    if (at < 0) return text.slice(0, 140);
    const start = Math.max(0, at - 60);
    const prefix = start > 0 ? "…" : "";
    return prefix + text.slice(start, start + 160);
  }

  function mark(text, terms) {
    const esc = text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    let out = esc;
    for (const term of terms) {
      if (!term) continue;
      const re = new RegExp("(" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      out = out.replace(re, "<mark>$1</mark>");
    }
    return out;
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
          entry.url +
          "</span>" +
          "<span class=\"snippet\">" +
          mark(snippet(entry.text, terms), terms) +
          "</span>" +
          "</a></li>"
        );
      })
      .join("");
    results.hidden = false;
  }

  function search(query) {
    const terms = words(query);
    if (terms.length === 0) {
      results.hidden = true;
      results.innerHTML = "";
      return;
    }
    load().then((data) => {
      const matches = data
        .map((entry) => ({ entry, s: score(entry, terms) }))
        .filter((m) => m.s >= 0)
        .sort((a, b) => b.s - a.s)
        .map((m) => m.entry);
      render(matches, terms);
    });
  }

  input.addEventListener("focus", load);
  input.addEventListener("input", () => search(input.value));

  input.addEventListener("keydown", (event) => {
    const items = results.querySelectorAll("li a");
    if (event.key === "Escape") {
      results.hidden = true;
      return;
    }
    if (event.key === "ArrowDown" && items.length) {
      event.preventDefault();
      active = Math.min(active + 1, items.length - 1);
    } else if (event.key === "ArrowUp" && items.length) {
      event.preventDefault();
      active = Math.max(active - 1, 0);
    } else if (event.key === "Enter" && active >= 0 && items[active]) {
      event.preventDefault();
      window.location.href = items[active].href;
      return;
    } else {
      return;
    }
    items.forEach((a, i) => a.parentElement.classList.toggle("active", i === active));
    items[active].scrollIntoView({ block: "nearest" });
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".search")) results.hidden = true;
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.target === input) return;
    const tag = event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || event.target.isContentEditable) return;
    event.preventDefault();
    input.focus();
  });
})();
