import { defineConfig } from "vite";

const proxy = {
  "/api": { target: "http://127.0.0.1:8000", ws: true },
};

export default defineConfig({
  define: { CESIUM_BASE_URL: JSON.stringify("/cesium/") },
  server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
});
