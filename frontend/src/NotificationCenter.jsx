// PWA 通知センター画面 (mode=notify で起動)。
// GitHub Notifications + iOS 通知センターの混血 UI。
// - 全セッションの ARK / ARIA 通知を時系列で集約
// - タップで app://chat/<sid> deep link で App (native) アプリへ遷移
// - 既読 / 未読、 一括既読、 削除
// - SSE で他クライアント (App) からの既読化をリアルタイム反映
import { useEffect, useState, useCallback, useRef } from 'react'
import { API_BASE } from './constants.js'
import { setBadge } from './utils/badge.js'
import { enablePush, isPushEnabledLocally } from './utils/push.js'
import './NotificationCenter.css'

function formatTime(ts) {
  const now = Date.now() / 1000
  const diff = now - ts
  if (diff < 60) return 'たった今'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 時間前`
  const d = new Date(ts * 1000)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${mm}/${dd} ${hh}:${mi}`
}

export default function NotificationCenter() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [pushEnabled, setPushEnabled] = useState(() => isPushEnabledLocally())
  const [pushBusy, setPushBusy] = useState(false)
  const [pushMsg, setPushMsg] = useState(null)
  const sseRef = useRef(null)

  // 初期取得
  const fetchAll = useCallback(async () => {
    try {
      setLoading(true)
      const res = await fetch(`${API_BASE}/notifications?limit=100`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setItems(data.notifications || [])
      // backend から取った unread_count でバッジ同期 (画面と完全一致)
      if (typeof data.unread_count === 'number') setBadge(data.unread_count)
      setError(null)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchAll() }, [fetchAll])

  // SW から ?deep=1&ses=xxx で起動された場合は即 App (native) アプリへリダイレクト。
  // 「OS 通知タップ → PWA が一瞬出る → App 起動」 の bridge ロジック。
  // PWA 自体は表示し続ける (戻るボタンで戻れる)。
  useEffect(() => {
    try {
      const sp = new URLSearchParams(window.location.search)
      if (sp.get('deep') === '1') {
        const sid = sp.get('ses')
        const target = sid ? `app://chat/${sid}` : 'app://'
        // クエリを掃除してから飛ばす (戻った時に無限ループしない)
        const url = new URL(window.location.href)
        url.searchParams.delete('deep')
        window.history.replaceState({}, '', url.toString())
        // 即リダイレクト
        window.location.href = target
      }
    } catch { /* ignore */ }
  }, [])

  // フォアグラウンド復帰時にバッジ再同期 (= バックグラウンド中に push が来てて、
  // SW がバッジ更新してたケースとの差を埋める)
  useEffect(() => {
    const onVis = () => {
      if (!document.hidden) fetchAll()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [fetchAll])

  // items 変化のたびにバッジを items から再計算して更新 (= UI と完全一致)
  useEffect(() => {
    const unread = items.filter(n => !n.read).length
    setBadge(unread)
  }, [items])

  // SSE: backend からの追加 / 既読化 / 削除を受信して UI 即時更新
  useEffect(() => {
    let es = null
    try {
      es = new EventSource(`${API_BASE}/notifications/stream`)
      sseRef.current = es
      es.onmessage = (ev) => {
        try {
          const event = JSON.parse(ev.data)
          if (event.type === 'added' && event.notification) {
            setItems(prev => [event.notification, ...prev])
          } else if (event.type === 'read' && Array.isArray(event.ids)) {
            const idSet = new Set(event.ids)
            setItems(prev => prev.map(n => idSet.has(n.id) ? { ...n, read: true } : n))
          } else if (event.type === 'removed' && Array.isArray(event.ids)) {
            const idSet = new Set(event.ids)
            setItems(prev => prev.filter(n => !idSet.has(n.id)))
          }
        } catch { /* ignore */ }
      }
      es.onerror = () => { /* EventSource は自動再接続するので noop */ }
    } catch { /* ignore */ }
    return () => { if (es) es.close() }
  }, [])

  const openItem = useCallback(async (item) => {
    // 既読化
    if (!item.read) {
      try {
        await fetch(`${API_BASE}/notifications/${encodeURIComponent(item.id)}/read`, { method: 'POST' })
      } catch { /* ignore */ }
      setItems(prev => prev.map(n => n.id === item.id ? { ...n, read: true } : n))
    }
    // App (native) へ deep link、 失敗したら PWA chat (?mode=chat&ses=) で代替
    const sid = item.session_id
    if (sid) {
      const native = `app://chat/${sid}`
      const fallback = `/?ses=${encodeURIComponent(sid)}`
      try {
        // location.href で app:// を呼ぶ → App アプリ起動
        window.location.href = native
        // 起動失敗時の fallback を遅延設定 (1.5 秒経っても起動してなければ PWA 開く)
        setTimeout(() => {
          // すでに別アプリに遷移してれば実行されない
          if (!document.hidden) window.location.href = fallback
        }, 1500)
      } catch {
        window.location.href = fallback
      }
    }
  }, [])

  const removeItem = useCallback(async (item, ev) => {
    ev?.stopPropagation()
    try {
      await fetch(`${API_BASE}/notifications/${encodeURIComponent(item.id)}`, { method: 'DELETE' })
    } catch { /* ignore */ }
    setItems(prev => prev.filter(n => n.id !== item.id))
  }, [])

  // プッシュ通知有効化: enablePush で許可 + subscribe
  const handleEnablePush = useCallback(async () => {
    setPushBusy(true)
    setPushMsg(null)
    try {
      await enablePush()
      setPushEnabled(true)
      setPushMsg('通知を有効化しました')
    } catch (e) {
      setPushMsg(`エラー: ${e.message || e}`)
    } finally {
      setPushBusy(false)
    }
  }, [])

  // リセット: SW / cache / pushMsg を消して再読み込み (= 最新コードを強制適用)
  // localStorage / IndexedDB / 通知許可は触らない (= 状態は保持)
  const handleReset = useCallback(async () => {
    setPushBusy(true)
    try {
      if ('serviceWorker' in navigator) {
        const regs = await navigator.serviceWorker.getRegistrations()
        await Promise.all(regs.map(r => r.unregister().catch(() => {})))
      }
      if (typeof caches !== 'undefined') {
        const keys = await caches.keys()
        await Promise.all(keys.map(k => caches.delete(k).catch(() => {})))
      }
    } catch { /* ignore */ }
    // cache buster で reload
    const url = new URL(window.location.href)
    url.searchParams.set('_r', String(Date.now()))
    window.location.replace(url.toString())
  }, [])

  const markAllRead = useCallback(async () => {
    try {
      await fetch(`${API_BASE}/notifications/read-all`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
    } catch { /* ignore */ }
    setItems(prev => prev.map(n => ({ ...n, read: true })))
  }, [])

  const unreadCount = items.filter(n => !n.read).length

  return (
    <div className="nc-app">
      <div className="nc-header">
        <span className="nc-title">🔔 App 通知</span>
        {unreadCount > 0 && <span className="nc-unread-badge">{unreadCount}</span>}
      </div>

      <div className="nc-toolbar">
        {!pushEnabled && (
          <button
            className="nc-tool-btn primary"
            onClick={handleEnablePush}
            disabled={pushBusy}
          >
            🔔 プッシュ通知を有効化
          </button>
        )}
        {pushEnabled && (
          <span className="nc-tool-status">✓ 通知有効</span>
        )}
        <button
          className="nc-tool-btn"
          onClick={handleReset}
          disabled={pushBusy}
          title="SW / cache を削除して最新コードを再読込 (履歴・通知許可は保持)"
        >
          ↺ リセット
        </button>
      </div>

      {pushMsg && <div className="nc-push-msg">{pushMsg}</div>}

      <div className="nc-actionbar">
        <span className="nc-count">
          {loading ? '読み込み中…' : items.length === 0 ? '通知なし' : `${items.length} 件 (新着 ${unreadCount})`}
        </span>
        <button className="nc-action-btn" onClick={markAllRead} disabled={unreadCount === 0}>
          全て既読
        </button>
        <button className="nc-action-btn" onClick={fetchAll} disabled={loading}>
          ↻
        </button>
      </div>

      {error && <div className="nc-error">取得失敗: {error}</div>}

      <div className="nc-list">
        {!loading && items.length === 0 && (
          <div className="nc-empty">
            <div className="nc-empty-icon">🌙</div>
            <div className="nc-empty-text">通知はありません</div>
            <div className="nc-empty-hint">App アプリで会話を進めると、 ここに集約されます。</div>
          </div>
        )}
        {items.map(item => (
          <div
            key={item.id}
            className={`nc-item ${item.read ? '' : 'unread'}`}
            onClick={() => openItem(item)}
          >
            <div className="nc-item-mark">{item.read ? '' : '●'}</div>
            <div className="nc-item-body">
              <div className="nc-item-head">
                <span className="nc-item-title">{item.title}</span>
                <span className="nc-item-time">{formatTime(item.ts)}</span>
              </div>
              <div className="nc-item-text">{item.body}</div>
              {item.session_id && (
                <div className="nc-item-meta">{item.session_id}</div>
              )}
            </div>
            <button
              className="nc-item-delete"
              onClick={(ev) => removeItem(item, ev)}
              aria-label="削除"
              title="削除"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
