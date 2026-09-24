import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The build output is committed (see web/README.md). `emptyOutDir` because the
// output directory is outside `web/`, and fixed asset names rather than content
// hashes so a rebuild is one diff on one file.
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
    sourcemap: false,
    // The console's browser floor — see web/README.md.
    target: ["chrome111", "edge111", "firefox114", "safari16.4", "ios16.4"],
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/app.[ext]",
      },
    },
  },
  server: {
    // One origin for both halves under `npm run dev`: the token cookie is
    // `SameSite=Strict` and would not be sent across two.
    proxy: {
      "/api": { target: "http://127.0.0.1:8123", ws: true, changeOrigin: false },
      "/perf": { target: "http://127.0.0.1:8123", changeOrigin: false },
      "/status": { target: "http://127.0.0.1:8123", changeOrigin: false },
    },
  },
});
