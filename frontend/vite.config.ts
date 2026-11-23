import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // 引入 Tailwind CSS v4 Vite 插件，与 React 插件并行编译原子样式与主题变量。
  plugins: [tailwindcss(), react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        // 保留浏览器 Host 与 Origin，让后端现有的同源写请求校验继续生效。
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.test.{ts,tsx}"],
    clearMocks: true,
    restoreMocks: true,
  },
});
