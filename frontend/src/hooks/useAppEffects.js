// App.jsx から責務分離した小粒 hook 群 (= push 状態同期、 既読化、 バッジ、 deep link 等)。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE, LS_SESSION_ACTIVITY } from '../constants.js'


// --- /push/state を可視状態 + active session で backend に申告 ---
// broadcast_push 抑制用: 「該当 session を見てる時は通知しない」 判定材料を backend に渡す。
export function usePushState(activeSid) {
  useEffect(() => {
    const sendState = () => {
      const body = JSON.stringify({
        visible: !document.hidden,
        session_id: activeSid,
        client: 'web',
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


// --- session を開いた時に既読化 (= backend 側の通知履歴を消す) ---
// アプリバッジ数字は App.jsx で useSessionBadges.unreadCount → setBadge 経路で
// 同期するので、 ここでは backend の read-all を投げるだけ (= push 通知センター用)。
export function useReadOnSessionOpen(activeSid) {
  useEffect(() => {
    if (!activeSid) return
    fetch(`${API_BASE}/notifications/read-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: activeSid }),
    }).catch(() => { /* ignore */ })
  }, [activeSid])
}


// --- PWA 通知から ?ses=xxx URL で該当 session に切替 ---
// 一度読んでから history.replaceState で URL から除去する。
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

  // messages dict は streaming flush で rAF 毎に新 reference になるが、 各 sid の
  // length が変化しない限りこの effect は走らせたくない。 length signature を計算して
  // dep にすることで、 reference 変化だけの再発火を抑える。
  const messagesLenSig = useMemo(
    () => Object.entries(messages).map(([sid, arr]) => `${sid}:${(arr || []).length}`).join('|'),
    [messages]
  )
  const messagesRef = useRef(messages)
  useEffect(() => { messagesRef.current = messages }, [messages])

  useEffect(() => {
    const cur = messagesRef.current
    setSessionActivity(prev => {
      let changed = false
      const next = { ...prev }
      const now = Date.now()
      for (const sid of Object.keys(cur)) {
        const arr = cur[sid] || []
        const curEntry = next[sid]
        if (!curEntry) {
          if (arr.length > 0) {
            next[sid] = { length: arr.length, ts: 0 }
            changed = true
          }
          continue
        }
        if (arr.length > curEntry.length) {
          next[sid] = { length: arr.length, ts: now }
          changed = true
        } else if (arr.length < curEntry.length) {
          next[sid] = { length: arr.length, ts: curEntry.ts }
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [messagesLenSig])

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
// 「最後に見た時の messages.length」 を localStorage に永続化することで、 リロード越しでも
// 既読状態が消えない + state 更新の race condition で「タップしたのに赤丸残る」 を防ぐ。
// 明示的な markAsSeen(sid) も expose し、 session click handler から二重に確実化する。
const LS_LAST_SEEN_LEN = 'cpc.lastSeenLen'

function loadLastSeen() {
  try {
    const raw = localStorage.getItem(LS_LAST_SEEN_LEN)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') return parsed
    }
  } catch { /* ignore */ }
  return {}
}

export function useSessionBadges({ sids, activeSid, messages, loading }) {
  const [lastSeenLen, setLastSeenLen] = useState(loadLastSeen)
  // messages の最新 ref (= markAsSeen で render 中にも参照する用)
  const messagesRef = useRef(messages)
  useEffect(() => { messagesRef.current = messages }, [messages])

  // localStorage 永続化
  useEffect(() => {
    try { localStorage.setItem(LS_LAST_SEEN_LEN, JSON.stringify(lastSeenLen)) } catch { /* ignore */ }
  }, [lastSeenLen])

  // 明示的既読化: session click 時に呼ばれる。 activeSid の useEffect が走る前に
  // sync で lastSeen を確定するので、 タップ → 別タブ切替の高速操作でも漏れない。
  const markAsSeen = useCallback((sid) => {
    if (!sid) return
    const len = (messagesRef.current[sid] || []).length
    setLastSeenLen(prev => (prev[sid] === len ? prev : { ...prev, [sid]: len }))
  }, [])

  // length signature: messages reference 変化ではなく、 実際に length が変わった時だけ
  // 下の useEffect を発火させる。 streaming flush で rAF 毎に messages reference が変わる
  // のを吸収するためのキー。
  const activeMsgLen = activeSid ? (messages[activeSid] || []).length : 0
  const messagesLenSig = useMemo(
    () => sids.map(sid => `${sid}:${(messages[sid] || []).length}`).join('|'),
    [sids, messages]
  )

  // active 会話: 表示中セッションの length が変わった時だけ lastSeen を最新化
  useEffect(() => {
    if (!activeSid) return
    setLastSeenLen(prev => (prev[activeSid] === activeMsgLen ? prev : { ...prev, [activeSid]: activeMsgLen }))
  }, [activeSid, activeMsgLen])

  // 削除された session の lastSeen 掃除 + 新規 / 未初期化 sid は現在 length で seed。
  // messagesRef 経由で最新値を読み取り (= dep に messages 直接置かない)。
  useEffect(() => {
    const cur = messagesRef.current
    setLastSeenLen(prev => {
      const sidSet = new Set(sids)
      const next = { ...prev }
      let changed = false
      for (const k of Object.keys(next)) {
        if (!sidSet.has(k)) { delete next[k]; changed = true }
      }
      for (const sid of sids) {
        if (next[sid] == null) {
          next[sid] = (cur[sid] || []).length
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [sids, messagesLenSig])

  // 各 session の表示状態 signature: length + pending question 有無 + loading 状態を
  // 1 つの string に圧縮。 messages dict reference が rAF 毎に変わっても、 実効状態が
  // 変わらない限り signature は同値 → 下の useMemo は dep 不変判定で同じ object を返し、
  // SessionDrawer 等下流の不要な re-render を抑える。
  const sessionStateSig = useMemo(
    () => sids.map(sid => {
      const arr = messages[sid] || []
      const pending = arr.some(m => m.askUserQuestion && !m.askUserQuestion.answered)
      return `${sid}:${arr.length}:${pending ? 'p' : ''}:${loading[sid] ? 'l' : ''}`
    }).join('|'),
    [sids, messages, loading]
  )

  // sessionBadges / unreadCount: signature が同じ間は同じ object を返す。
  // unreadCount はアプリバッジ用 = 「新着 (= 赤丸)」 のみカウント。 処理中 (= 青丸) や
  // 質問待ち (= ?) はバッジに含めない仕様。
  const { sessionBadges, unreadCount } = useMemo(() => {
    const cur = messagesRef.current
    const badges = {}
    let count = 0
    for (const sid of sids) {
      if (sid === activeSid) { badges[sid] = null; continue }
      const arr = cur[sid] || []
      const pending = arr.some(m => m.askUserQuestion && !m.askUserQuestion.answered)
      if (pending) { badges[sid] = { kind: 'pending', label: '?' }; continue }
      if (loading[sid]) { badges[sid] = { kind: 'processing', label: '●' }; continue }
      const lastSeen = lastSeenLen[sid] ?? arr.length
      if (arr.length > lastSeen) { badges[sid] = { kind: 'new', label: '●' }; count++; continue }
      badges[sid] = null
    }
    return { sessionBadges: badges, unreadCount: count }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSid, sessionStateSig, lastSeenLen])
  return { sessionBadges, unreadCount, markAsSeen }
}


