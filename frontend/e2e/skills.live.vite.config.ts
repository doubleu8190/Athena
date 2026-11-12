import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { readSkillsLiveContract } from "./skills.live.contract";

const contract = readSkillsLiveContract();
const frontendURL = new URL(contract.frontend_base_url);
const apiURL = new URL(contract.api_base_url);

export default defineConfig({
  plugins: [react()],
  server: {
    host: frontendURL.hostname,
    port: Number(frontendURL.port),
    proxy: {
      "/api": {
        target: apiURL.origin,
        // 保留浏览器 Host 与 Origin，供后端同源写请求校验。
        changeOrigin: false,
      },
    },
    strictPort: true,
    watch: {
      ignored: [
        "**/playwright-report/**",
        "**/test-results/**",
        "**/../var/skills-e2e/**",
      ],
    },
  },
});
