import { defineConfig } from 'vitest/config'

// アプリの vite.config.js は config 時に manifest.json を生成する副作用があるため、
// テストはそれを読まない独立 config にする。 全テストは node 環境で完結する
// (= DOM 依存は localStorage 等を in-memory スタブで差し替える方針)。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.{js,jsx}'],
  },
})
