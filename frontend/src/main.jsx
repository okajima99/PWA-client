import { StrictMode, lazy, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'

// Service Worker 登録 (Web Push 受信用)。
// iOS PWA は 16.4+ かつホーム画面追加済みでのみ Push を受け取れる。
// 未対応環境では何もしない。
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* noop */ })
  })
}

// 役割分担: PWA = 通知センター、 App (native) = チャット + 画面共有
// 起動時に platform で default mode を分岐:
//   - native (Capacitor) → chat (チャット UI)
//   - web (PWA) → notify (通知センター)
// URL に ?mode=xxx があれば上書き (debug 用)
function getMode() {
  try {
    const sp = new URLSearchParams(window.location.search)
    const fromUrl = sp.get('mode')
    if (fromUrl) return fromUrl
    if (window.Capacitor?.isNativePlatform?.() === true) return 'chat'
    return 'notify'
  } catch {
    return 'notify'
  }
}

const NotificationCenter = lazy(() => import('./NotificationCenter.jsx'))

const mode = getMode()
const Root = mode === 'notify'
  ? (
      <Suspense fallback={<div style={{ padding: 20, color: '#888' }}>読み込み中…</div>}>
        <NotificationCenter />
      </Suspense>
    )
  : <App />

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      {Root}
    </ErrorBoundary>
  </StrictMode>,
)
