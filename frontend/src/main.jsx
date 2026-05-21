import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
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

// PTY 経路 dev エスケープハッチ: URL に `?terminal=<sessionId>` を付けると xterm.js
// 1 画面に切替 (= 旧 chat UI を一切 mount せずに新経路を試せる)。 Phase 4 完了で削除予定。
const terminalSessionId = (() => {
  const sid = new URLSearchParams(window.location.search).get('terminal')
  return sid && sid.trim() ? sid.trim() : null
})()

// PWA は chat 単一画面 (= 2026-05-16 で mode 分岐撤去 + 通知センター撤去、 未読数だけ
// app badge 用に維持)。
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      {terminalSessionId ? (
        <div style={{ position: 'fixed', inset: 0, background: '#0e0f12' }}>
          <Terminal sessionId={terminalSessionId} />
        </div>
      ) : (
        <App />
      )}
    </ErrorBoundary>
  </StrictMode>,
)
