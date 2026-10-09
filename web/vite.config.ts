/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발 서버는 FastAPI(127.0.0.1:8787)로 /api 를 프록시한다. 빌드 결과(dist/)는 FastAPI 가 서빙한다.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8787" } },
  build: { outDir: "dist" },
  test: { environment: "jsdom", globals: false },
});
