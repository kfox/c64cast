import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Separate from vite.config.ts on purpose: the app build needs the Svelte and
// Tailwind plugins, and this needs neither yet — everything under test today
// is plain TypeScript logic pulled out of a component specifically so it
// doesn't need one mounted. Shares only the one thing that matters for
// imports to resolve: the `$lib` alias.
export default defineConfig({
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL("./src/lib", import.meta.url)),
    },
  },
  test: {
    environment: "node",
  },
});
