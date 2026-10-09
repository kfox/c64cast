- **A search box on the documentation site.** Every page at
  <https://kfox.github.io/c64cast/> now carries a search field in the header
  that matches against every book chapter and standalone doc, ranking a title
  hit over a body hit, and jumps straight to the page on Enter or a click.
  It is client-side against a JSON index `scripts/build_site.py` writes at
  build time — no server, no third-party search service, no page reload.
  Press `/` anywhere on the site to focus it.
