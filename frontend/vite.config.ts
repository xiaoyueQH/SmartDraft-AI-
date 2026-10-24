import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    port: 5183,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.SUPPORTFLOW_API_TARGET || "http://127.0.0.1:8091",
        changeOrigin: false,
      },
    },
  },
  preview: { host: "127.0.0.1", port: 4183, strictPort: true },
});
