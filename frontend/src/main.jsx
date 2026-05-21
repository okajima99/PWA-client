import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import TerminalApp from './TerminalApp.jsx'
import Terminal from './components/Terminal.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'

// Service Worker 登録 (Web Push 受信用)。
// iOS PWA は 16.4+ かつホーム画面追加済みでのみ Push を受け取れる。
// 未対応環境では何もしない。
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* noop */ })
  })
}

// ルーティング:
//   `?terminal=<id>`      → xterm.js single-shot (= debug / 直リンク用)
//   `?legacy=1`           → 旧 chat UI (= 移行期間の保険、 後で削除予定)
//   それ以外              → TerminalApp (= 既存 UI + Terminal 差し替え)
const params = new URLSearchParams(window.location.search)
const terminalSessionId = (() => {
  const sid = params.get('terminal')
  return sid && sid.trim() ? sid.trim() : null
})()
const legacyMode = params.get('legacy') === '1'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      {terminalSessionId ? (
        <div style={{ position: 'fixed', inset: 0, background: '#0e0f12' }}>
          <Terminal sessionId={terminalSessionId} />
        </div>
      ) : legacyMode ? (
        <App />
      ) : (
        <TerminalApp />
      )}
    </ErrorBoundary>
  </StrictMode>,
)
