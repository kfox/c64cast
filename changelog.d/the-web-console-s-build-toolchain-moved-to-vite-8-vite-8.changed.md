- **The web console's build toolchain moved to Vite 8.** Vite 8 bundles with
  Rolldown and minifies stylesheets with Lightning CSS, where Vite 7 used
  Rollup and esbuild, so the committed bundle is rebuilt here with no source
  change behind it. Vite's default `baseline-widely-available` target moved
  with it — from Chrome 107, Edge 107, Firefox 104 and Safari 16 to Chrome 111,
  Edge 111, Firefox 114 and Safari/iOS 16.4 — and both the script and the
  stylesheet are emitted for it, so that is the floor the console's build now
  targets. One consequence is visible in the stylesheet: the two responsive
  breakpoints ship as media-query range syntax, `(width >= 40rem)` in place of
  `(min-width: 40rem)`, which Safari and iOS below 16.4 match at no width,
  leaving them on the narrow layout. That floor is now written out in
  `web/vite.config.ts` rather than inherited from Vite, so a later Vite release
  does not move it on its own.
