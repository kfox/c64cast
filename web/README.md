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

`.node-version` at the repo root is the Node this builds on. CI's
`actions/setup-node` reads it, as does fnm; mise reads it because `mise.toml`
enables the `node` idiomatic version file, and asdf reads it only with
`legacy_version_file = yes` in `~/.asdfrc`. nvm reads `.nvmrc` only, never this
file. Change it there and CI and a local rebuild move together.

`web/.npmrc` sets `ignore-scripts=true`, so no dependency's `preinstall`,
`install` or `postinstall` runs on an install here — the npm that the pinned
Node ships, which is what CI builds with, runs them by default. The same
setting makes `npm run X` skip `preX`/`postX`, which is why `package.json`
declares no such hook and `tests/test_web_toolchain.py` fails if one appears.

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

The build is deterministic: fixed asset names (`assets/app.js`, `assets/app.css`)
rather than content hashes, so a rebuild is one diff on one file instead of a
new file plus an orphan. `web_static.py` serves them `no-cache` for the same
reason.

The console's browser floor is `build.target` in `vite.config.ts`, stated there
rather than inherited: Vite's `baseline-widely-available` default moves forward
with Vite, and the 7 to 8 bump raised the floor with no source change behind it.
Both the JavaScript minifier and Lightning CSS emit for that value — it decides
whether a breakpoint ships as `(width >= 40rem)` or as `(min-width: 40rem)` —
so editing it moves the bundle, and the diff above is what makes the move
visible. `tests/test_web_toolchain.py` fails if the value goes missing, which a
rebuild cannot notice for as long as Vite's default still matches it.

Moving the floor is that edit plus a rebuild, in one commit. What the installed
Vite would default to on its own, for choosing the value:

```bash
cd web && node --input-type=module -e \
  "import { resolveConfig } from 'vite'; \
   console.log((await resolveConfig({ configFile: false }, 'build')).build.target)"
```

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

## Layout

| Path | What |
|---|---|
| `src/App.svelte` | The shell: owns the one state feed and the router, and hands both down |
| `src/lib/api.ts` | `fetch` wrappers; `ApiError` carries the status and the body |
| `src/lib/actions.ts` | Cross-screen actions (`launch`) that touch `console.svelte.ts` state |
| `src/lib/console.svelte.ts` | The `/api/ws` feed as one reactive object |
| `src/lib/router.svelte.ts` | Which screen is showing, kept in the address bar |
| `src/lib/setup.ts` | The appliance first-run form's own three-call client — the only endpoints reachable without a token |
| `src/lib/configListLogic.ts` | `ConfigList`'s search/sort/name-display, as plain functions — see Testing |
| `src/lib/debounce.ts` | Coalesce a burst of calls into one — the media picker's search-as-you-type |
| `src/lib/errorsLogic.ts` | `describeError`: the one status-code→sentence mapping every screen's `problem` line uses |
| `src/lib/liveKeysLogic.ts` | Live's keyboard shortcuts: key → command, as plain functions — see Testing |
| `src/lib/uploadLogic.ts` | The upload progress line and percentage, as plain functions |
| `src/lib/introspect.ts` | `/api/introspect`, fetched once, indexed for lookup |
| `src/lib/types.ts` | Hand-written mirrors of the daemon's JSON |
| `src/lib/components/` | Presentational pieces |
| `src/lib/components/FieldInput.svelte` | One config field's control, chosen by its declared type |
| `src/lib/screens/` | One file per screen |
| `src/lib/screens/Setup.svelte` | The appliance first-run form. Mounted by `main.ts` *instead of* the shell — see below |

Two rules the screens follow and a new one should too. Panels are `min-w-0`
grid items — a grid item is min-content-sized by default, so one long log line
otherwise makes the whole *page* wider than the phone reading it. And a control
under the finger ignores the echo: values round-trip through the parent and come
back formatted, so a text input holds its raw text while it has the caret
(`FieldInput`) and a slider holds its position through a gesture (`FxSlider`).

## The first-run form

`main.ts` asks `GET /api/setup` once before it mounts anything, and mounts
`screens/Setup.svelte` rather than `App.svelte` when the host answers "pending".
That probe exists because while the appliance setup window is open
([`setup_gate.py`](../c64cast/control/setup_gate.py)) *every* other route
answers `503` — a console that mounted first would come up with nothing but
errors. Anything other than "pending" (including the `401` an ordinary host
answers, and no answer at all from one still coming up) mounts the console.

The form is a screen of this same bundle rather than a page the server renders,
because the gate deliberately leaves the shell and `/assets` reachable while
everything else is blocked. It never sees the host's token: the host reports
only whether one may be *set*, and hands the real one back exactly once, in the
`login_url` of a completed submission, which the page then navigates to.

Design notes — why the bundle is committed, why the fallback is a catch-all,
why the assets are served by hand — are in
[`c64cast/control/web_static.py`](../c64cast/control/web_static.py) and
[`docs/architecture/control.md`](../docs/architecture/control.md).
