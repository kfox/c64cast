# The c64cast web console

Source for the browser UI that `c64cast --serve` hosts. Svelte 5 (runes) +
Vite + TypeScript + Tailwind v4.

**The build output is committed**, at `c64cast/web/dist/`, and ships as package
data. That is the point: a `uv sync` install has no Node, no npm and often no
route to a registry, and the console still has to come up. Node is required to
*change* the UI, never to run it.

## Working on it

```bash
cd web
npm install
npm run dev        # http://localhost:5173, proxying /api to the daemon
```

Run the daemon alongside it, on its default port:

```bash
python -m c64cast --serve -u u64://HOST
```

`npm run dev` forwards `/api`, `/perf` and `/status` to `127.0.0.1:8123` so both
halves share one origin — which matters, because the token cookie is
`SameSite=Strict` and would not be sent across two.

## Shipping a change

```bash
make web           # from the repo root: svelte-check, vite build, then npm test
git add web c64cast/web/dist
```

The bundle **must be rebuilt in the same commit as the source**. CI rebuilds it
and fails on a diff, the same way it does for the JSON schema and the generated
reference appendices.

## Testing

```bash
cd web
npm test           # vitest, once (make web runs this too)
npx vitest         # watch mode
```

Pure logic pulled out of a component — `*.test.ts` beside the module it tests,
e.g. `src/lib/configListLogic.test.ts`. Plain `vitest`, no DOM: everything
under test today is logic a component imports rather than a component itself,
which is why there's no `@testing-library/svelte` here yet. `vitest.config.ts`
is deliberately separate from `vite.config.ts` — the app build needs the Svelte
and Tailwind plugins, and a logic-only test needs neither.

The build is deterministic: fixed asset names (`assets/app.js`, `assets/app.css`)
rather than content hashes, so a rebuild is one diff on one file instead of a
new file plus an orphan. `web_static.py` serves them `no-cache` for the same
reason.

## Layout

| Path | What |
|---|---|
| `src/App.svelte` | The shell: owns the one state feed and the router, and hands both down |
| `src/lib/api.ts` | `fetch` wrappers; `ApiError` carries the status and the body |
| `src/lib/actions.ts` | Cross-screen actions (`launch`) that touch `console.svelte.ts` state |
| `src/lib/console.svelte.ts` | The `/api/ws` feed as one reactive object |
| `src/lib/router.svelte.ts` | Which screen is showing, kept in the address bar |
| `src/lib/configListLogic.ts` | `ConfigList`'s search/sort/name-display, as plain functions — see Testing |
| `src/lib/uploadLogic.ts` | The upload progress line and percentage, as plain functions |
| `src/lib/introspect.ts` | `/api/introspect`, fetched once, indexed for lookup |
| `src/lib/types.ts` | Hand-written mirrors of the daemon's JSON |
| `src/lib/components/` | Presentational pieces |
| `src/lib/components/FieldInput.svelte` | One config field's control, chosen by its declared type |
| `src/lib/screens/` | One file per screen |

Two rules the screens follow and a new one should too. Panels are `min-w-0`
grid items — a grid item is min-content-sized by default, so one long log line
otherwise makes the whole *page* wider than the phone reading it. And a control
under the finger ignores the echo: values round-trip through the parent and come
back formatted, so a text input holds its raw text while it has the caret
(`FieldInput`) and a slider holds its position through a gesture (`FxSlider`).

Design notes — why the bundle is committed, why the fallback is a catch-all,
why the assets are served by hand — are in
[`c64cast/control/web_static.py`](../c64cast/control/web_static.py) and
[`docs/architecture/control.md`](../docs/architecture/control.md).
