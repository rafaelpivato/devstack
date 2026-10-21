import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

const entry = fileURLToPath(new URL("./src/main.tsx", import.meta.url));
const output = fileURLToPath(new URL("../src/devstack/static/v06", import.meta.url));

export default defineConfig({
  base: "/static/v06/",
  build: {
    outDir: output,
    emptyOutDir: true,
    manifest: "manifest.json",
    cssCodeSplit: false,
    rollupOptions: { input: entry },
  },
});
