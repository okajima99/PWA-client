// App.jsx から責務分離した小粒 hook 群。
// 1 hook あたり数十行、 App.jsx の useEffect / useState の塊を別 module に外出しして
// メイン本体を 1088 行から ~620 行に縮小する目的。
import { useEffect, useRef, useCallback, useState } from 'react'
import { API_BASE, LS_SESSION_ACTIVITY } from '../constants.js'
import { syncBadgeFromServer } from '../utils/badge.js'


// --- /push/state を可視状態 + active session で backend に申告 ---
// broadcast_push 抑制用: 「App (native) / PWA (web) のいずれかが該当 session を
// 見てる時は通知しない」 判定材料を backend に渡す。
export function usePushState(activeSid) {
  useEffect(() => {
    const sendState = () => {
      const isNative = !!window.Capacitor?.isNativePlatform?.()
      const body = JSON.stringify({
        visible: !document.hidden,
        session_id: activeSid,
        client: isNative ? 'native' : 'web',
      })
      try {
        fetch(`${API_BASE}/push/state`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
        }).catch(() => { /* ignore */ })
      } catch { /* ignore */ }
    }
    sendState()
    const onVis = () => sendState()
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [activeSid])
}


// --- session を開いた時に既読化 + バッジ再同期 ---
export function useReadOnSessionOpen(activeSid) {
  useEffect(() => {
    if (!activeSid) return
    ;(async () => {
      try {
        await fetch(`${API_BASE}/notifications/read-all`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: activeSid }),
        })
      } catch { /* ignore */ }
      try { await syncBadgeFromServer() } catch { /* ignore */ }
    })()
  }, [activeSid])
}


// --- 起動 + フォア復帰でバッジ再同期 ---
export function useBadgeSync() {
  useEffect(() => {
    syncBadgeFromServer().catch(() => {})
    const onVis = () => { if (!document.hidden) syncBadgeFromServer().catch(() => {}) }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])
}


// --- PWA 通知 / native deep link を受けて該当 session に切替 ---
// - PWA: ?ses=xxx URL を一度読んでから history.replaceState で除去
// - Native: Capacitor App の appUrlOpen で app://chat/<sid> を listen
export function useDeepLink(setActiveId) {
  useEffect(() => {
    try {
      const sp = new URLSearchParams(window.location.search)
      const sid = sp.get('ses')
      if (sid) {
        setActiveId(sid)
        const url = new URL(window.location.href)
        url.searchParams.delete('ses')
        window.history.replaceState({}, '', url.toString())
      }
    } catch { /* ignore */ }

    let cleanup = null
    ;(async () => {
      try {
        if (!window.Capacitor?.isNativePlatform?.()) return
        const { App: CapApp } = await import('@capacitor/app')
        const handler = ({ url }) => {
          try {
            const m = String(url || '').match(/^app:\/\/chat\/([\w-]+)/)
            if (m && m[1]) setActiveId(m[1])
          } catch { /* ignore */ }
        }
        const sub = await CapApp.addListener('appUrlOpen', handler)
        cleanup = () => { try { sub.remove() } catch { /* ignore */ } }
      } catch { /* @capacitor/app 未インストール環境では noop */ }
    })()
    return () => { if (cleanup) cleanup() }
  }, [setActiveId])
}


// --- session ごとの「最終活動時刻」 を localStorage に永続化 + 並び順 sort 用 ---
// 値: { length: 直近の messages 件数, ts: その時の Date.now() }
// 永続値が無ければ ts=0 で記録 (= sort では created_at fallback)。
export function useSessionActivity(messages, sessions) {
  const [sessionActivity, setSessionActivity] = useState(() => {
    try {
      const raw = localStorage.getItem(LS_SESSION_ACTIVITY)
      if (raw) {
        const parsed = JSON.parse(raw)
        if (parsed && typeof parsed === 'object') return parsed
      }
    } catch { /* ignore */ }
    return {}
  })

  useEffect(() => {
    setSessionActivity(prev => {
      let changed = false
      const next = { ...prev }
      const now = Date.now()
      for (const sid of Object.keys(messages)) {
        const arr = messages[sid] || []
        const cur = next[sid]
        if (!cur) {
          if (arr.length > 0) {
            next[sid] = { length: arr.length, ts: 0 }
            changed = true
          }
          continue
        }
        if (arr.length > cur.length) {
          next[sid] = { length: arr.length, ts: now }
          changed = true
        } else if (arr.length < cur.length) {
          next[sid] = { length: arr.length, ts: cur.ts }
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [messages])

  useEffect(() => {
    try { localStorage.setItem(LS_SESSION_ACTIVITY, JSON.stringify(sessionActivity)) } catch { /* ignore */ }
  }, [sessionActivity])

  // sort された session 一覧 (= 「最終活動時刻」 降順、 0 や未活動は created_at fallback)
  const sortedSessions = [...sessions].sort((a, b) => {
    const ta = (sessionActivity[a.id]?.ts) || ((a.created_at || 0) * 1000)
    const tb = (sessionActivity[b.id]?.ts) || ((b.created_at || 0) * 1000)
    return tb - ta
  })

  return { sessionActivity, sortedSessions }
}


// --- session ごとの新着 / 処理中 / 質問待ちバッジ計算 ---
// active session は常に lastSeen を最新化、 非 active で arr.length > lastSeen なら新着。
// 「最後に見た時の messages.length」 は state ではなく version 付き object にして、
// effect 内でだけ書き換える。 render 中は lastSeenLen state を読むだけにして
// react-hooks/refs ルール (= ref を render 中に触ると再 render されない) を避ける。
export function useSessionBadges({ sids, activeSid, messages, loading }) {
  const [lastSeenLen, setLastSeenLen] = useState({})

  // active 会話: messages 変化のたびに lastSeen を最新化
  useEffect(() => {
    if (!activeSid) return
    const len = (messages[activeSid] || []).length
    setLastSeenLen(prev => (prev[activeSid] === len ? prev : { ...prev, [activeSid]: len }))
  }, [activeSid, messages])

  // 削除された session の lastSeen 掃除 + 新規 / 未初期化 sid は現在 length で seed
  useEffect(() => {
    setLastSeenLen(prev => {
      const sidSet = new Set(sids)
      const next = { ...prev }
      let changed = false
      for (const k of Object.keys(next)) {
        if (!sidSet.has(k)) { delete next[k]; changed = true }
      }
      for (const sid of sids) {
        if (next[sid] == null) {
          next[sid] = (messages[sid] || []).length
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [sids, messages])

  const sessionBadges = {}
  for (const sid of sids) {
    if (sid === activeSid) { sessionBadges[sid] = null; continue }
    const arr = messages[sid] || []
    const pending = arr.some(m => m.askUserQuestion && !m.askUserQuestion.answered)
    if (pending) { sessionBadges[sid] = { kind: 'pending', label: '?' }; continue }
    if (loading[sid]) { sessionBadges[sid] = { kind: 'processing', label: '●' }; continue }
    const lastSeen = lastSeenLen[sid] ?? arr.length
    if (arr.length > lastSeen) { sessionBadges[sid] = { kind: 'new', label: '●' }; continue }
    sessionBadges[sid] = null
  }
  return sessionBadges
}


// --- IME 入力 hidden input の handler ---
// 「あ」 ボタンで focus、 iOS キーボード出る → 日本語確定 (compositionend) で
// Mac へ sendUtf8Text。 入力後は input.value をクリアして次の入力に備える。
export function useImeBridge() {
  const imeInputRef = useRef(null)
  const handleImeFocus = useCallback(() => {
    imeInputRef.current?.focus()
  }, [])
  const handleImeCompositionEnd = useCallback(async (e) => {
    const text = e.target.value
    if (!text) return
    try {
      const m = await import('../native/moonlight-flow.js')
      await m.sendUtf8Text(text)
    } catch { /* ignore */ }
    e.target.value = ''
  }, [])
  return { imeInputRef, handleImeFocus, handleImeCompositionEnd }
}


// --- 物理キーボード → Mac へ転送 (stream 接続中のみ、 chat 入力フォーカス時は除外) ---
export function usePhysicalKeyboardForward(streaming) {
  useEffect(() => {
    if (!window.Capacitor?.isNativePlatform?.()) return
    let mod = null
    let mappers = null
    ;(async () => {
      mod = await import('../native/moonlight-flow.js')
      mappers = await import('../native/keyboard-map.js')
    })()

    const isInputFocused = () => {
      const a = document.activeElement
      if (!a) return false
      const tag = a.tagName
      return tag === 'TEXTAREA' || tag === 'INPUT' || a.isContentEditable
    }

    const send = (action) => (ev) => {
      if (!streaming) return
      if (isInputFocused()) return
      if (!mod || !mappers) return
      const m = mappers.mapKeyEventToVK(ev)
      if (!m) return
      mod.sendKeyEvent(m.keyCode, m.modifiers, action).catch(() => {})
      ev.preventDefault()
    }

    const onDown = send('down')
    const onUp = send('up')
    document.addEventListener('keydown', onDown, { capture: true })
    document.addEventListener('keyup', onUp, { capture: true })
    return () => {
      document.removeEventListener('keydown', onDown, { capture: true })
      document.removeEventListener('keyup', onUp, { capture: true })
    }
  }, [streaming])
}
