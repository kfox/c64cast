import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The build output is committed (see web/README.md), which is what lets a
// `uv sync` install serve the console with no Node anywhere. Two consequences
// are configured here rather than left at Vite's defaults:
//
//   * `emptyOutDir` — the output directory lives inside the Python package, so
//     the build must be told it is allowed to clear a path outside `web/`.
//   * fixed asset names — content-hashed filenames would add a new file to git
//     on every build and leave the old one behind. Fixed names make a rebuild
//     one diff on one file, which is the only way a committed artifact stays
//     reviewable. The console is a LAN page served by a process that restarts
//     with the assets, so the cache-busting the hashes buy is worth nothing.
export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL("./src/lib", import.meta.url)),
    },
  },
  build: {
    outDir: "../c64cast/web/dist",
    emptyOutDir: true,
    // A LAN console on a Commodore-adjacent machine, not a public site: the
    // legibility of one readable bundle beats shaving a few KB.
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/app.[ext]",
      },
    },
  },
  server: {
    // `npm run dev` serves the UI with hot reload and forwards everything the
    // daemon owns, so the two halves can be developed against one origin —
    // which matters because the token cookie is `SameSite=Strict`.
    proxy: {
      "/api": { target: "http://127.0.0.1:8123", ws: true, changeOrigin: false },
      "/perf": { target: "http://127.0.0.1:8123", changeOrigin: false },
      "/status": { target: "http://127.0.0.1:8123", changeOrigin: false },
    },
  },
});
