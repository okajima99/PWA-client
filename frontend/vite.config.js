import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Generate public/manifest.json from manifest.template.json by injecting
// values from VITE_APP_* env vars. Runs once at config time so both `vite`
// (dev) and `vite build` pick up the latest values.
function generateManifest(env) {
  const root = resolve(import.meta.dirname, 'public')
  const template = readFileSync(resolve(root, 'manifest.template.json'), 'utf-8')
  const out = template
    .replace(/__APP_TITLE__/g, env.VITE_APP_TITLE || 'App')
    .replace(/__APP_SHORT_NAME__/g, env.VITE_APP_SHORT_NAME || 'App')
    .replace(/__APP_ICON_192__/g, env.VITE_APP_ICON_192 || '/icon-192.svg')
    .replace(/__APP_ICON_512__/g, env.VITE_APP_ICON_512 || '/icon-512.svg')
  writeFileSync(resolve(root, 'manifest.json'), out)
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, '')
  generateManifest(env)
  return {
    plugins: [react()],
    build: {
      // 初回 download を縮める chunk 分割。 react / @capacitor は安定で再利用可、
      // markdown 系は遅延 load 候補なので別 chunk に逃がして main を軽くする。
      rollupOptions: {
        output: {
          // vite 8 (rolldown) は manualChunks を function only でしか受け付けない。
          // node_modules の path から chunk 名を決める。
          manualChunks(id) {
            if (!id.includes('node_modules')) return undefined
            if (id.includes('react-syntax-highlighter') ||
                id.includes('react-markdown') ||
                id.includes('remark-gfm') ||
                id.includes('mdast') ||
                id.includes('micromark') ||
                id.includes('hast') ||
                id.includes('refractor') ||
                id.includes('prismjs')) return 'markdown'
            if (id.includes('@capacitor')) return 'capacitor'
            if (id.includes('lz-string')) return 'lz'
            if (id.includes('react-dom') || id.includes('/react/')) return 'react-vendor'
            return undefined
          },
        },
      },
      // chunk 警告を 600KB → 800KB (= markdown chunk が ~700KB あるため、 警告 spam 抑止)
      chunkSizeWarningLimit: 800,
    },
  }
})
