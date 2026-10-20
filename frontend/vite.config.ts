import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import { resolve } from "node:path"

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000"
const frontendPort = Number(process.env.FRONTEND_PORT || 5173)

export default defineConfig({
  // Electron loads the renderer with file://, so emitted assets must resolve
  // relative to index.html rather than from the host filesystem root.
  base: "./",
  plugins: [react()],
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  server: {
    port: frontendPort,
    proxy: {
      "/api": { target: apiProxyTarget, changeOrigin: true },
    },
  },
})
