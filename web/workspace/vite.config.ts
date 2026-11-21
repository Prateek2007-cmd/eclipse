import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/workspace/",
  build: {
    outDir: "dist",
    target: "esnext",
  },
  server: {
    port: 5173,
    proxy: {
      // Sprint 9 fix (review M8): the api client fetches root
      // paths (/tasks, /proof, …), not /api — so the dev proxy
      // must forward those too. Without this, `npm run dev`
      // returned the SPA fallback HTML for every data call and
      // res.json() threw.
      "/api": "http://127.0.0.1:8000",
      "/tasks": "http://127.0.0.1:8000",
      "/proof": "http://127.0.0.1:8000",
      "/audit": "http://127.0.0.1:8000",
      "/vault": "http://127.0.0.1:8000",
      "/monitor": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/metrics": "http://127.0.0.1:8000",
      "/quota": "http://127.0.0.1:8000",
      "/jobs": "http://127.0.0.1:8000",
      "/mail": "http://127.0.0.1:8000",
      "/rag": "http://127.0.0.1:8000",
      "/memory": "http://127.0.0.1:8000",
      "/connectors": "http://127.0.0.1:8000",
    },
  },
});
