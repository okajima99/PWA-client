// App.jsx から責務分離した小粒 hook 群 (= push 状態同期、 既読化、 バッジ、 deep link 等)。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { LS_SESSION_ACTIVITY } from '../constants.js'
import { apiFetch } from '../utils/api.js'
import { lsGet, lsSet } from '../utils/storage.js'
import { clearAllNotifications } from '../utils/badge.js'


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
        apiFetch(`/push/state`, {
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
    apiFetch(`/notifications/read-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: activeSid }),
    }).catch(() => { /* ignore */ })
  }, [activeSid])
}


// --- 画面共有 (= moonlight-web-stream) が利用可能かをマウント時に検出 ---
// Path B (= Sunshine + moonlight-web-stream セットアップ済) のユーザだけ
// 🖥 ボタンを表示する。 backend に対して `/moonlight/` への HEAD を 1 回投げて
// 2xx なら有効、 404 / network error なら無効と判定。 結果をマウント中保持。
export function useMoonlightAvailable() {
  const [available, setAvailable] = useState(false)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await apiFetch(`/moonlight/`, { method: 'HEAD', credentials: 'same-origin' })
        if (!cancelled) setAvailable(res.ok)
      } catch {
        if (!cancelled) setAvailable(false)
      }
    })()
    return () => { cancelled = true }
  }, [])
  return available
}


// --- PWA 起動時 / visibility 復帰時に通知センター + バッジ + backend カウンタを掃除 ---
// iOS PWA は通知センターに通知が残ってる間アプリバッジを「未読通知数」 で上書きする
// 挙動があるので、 通知本体を能動的に close しないとバッジが消えない。 backend の
// `unread_count` global も累積され続ける (= push のたびに +1) ため、 ここで sync で 0 に
// 上書きする。 backend が新たに push を飛ばすと再度カウントが立つ。
export function useNotificationClear() {
  useEffect(() => {
    // mount 時 1 回
    clearAllNotifications()
    // visibility 復帰時にも
    const onVis = () => {
      if (!document.hidden) clearAllNotifications()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])
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
    const parsed = lsGet(LS_SESSION_ACTIVITY)
    return parsed && typeof parsed === 'object' ? parsed : {}
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
    lsSet(LS_SESSION_ACTIVITY, sessionActivity)
  }, [sessionActivity])

  // sort された session 一覧 (= 「最終活動時刻」 降順、 0 や未活動は created_at fallback)。
  // sessions / sessionActivity が変わらない限り同じ array を返す (= SessionDrawer 等の
  // 下流が無駄に re-render しない)。
  const sortedSessions = useMemo(
    () => [...sessions].sort((a, b) => {
      const ta = (sessionActivity[a.id]?.ts) || ((a.created_at || 0) * 1000)
      const tb = (sessionActivity[b.id]?.ts) || ((b.created_at || 0) * 1000)
      return tb - ta
    }),
    [sessions, sessionActivity]
  )

  return { sessionActivity, sortedSessions }
}


// --- session ごとの新着 / 処理中 / 質問待ちバッジ計算 ---
// バッジは停止/送信ボタンの状態と 1:1 同期する (= 2026-05-29 改定):
//   - loading[sid] === true (= 停止ボタン中) → 青丸 (processing)
//   - loading[sid] が true→false に遷移 (= 送信解禁、 turn 完了) → 赤丸 (new)
//   - active タブで赤丸を見たら解除
// 旧仕様の `arr.length > lastSeen` は使わない: streaming 中の length 変動や JSONL flush の
// 順序揺らぎを噛むより、 loading 解除の 1 イベントで「返信きた」 を確定する方が体感に合う。
// 「turn 完了で未閲覧」 を localStorage に永続化 (= リロード跨ぎで赤を保持)。
const LS_UNREAD_DONE = 'cpc.unreadDone'

// 旧バッジ仕様 (= `arr.length > lastSeen`) の orphan key を 1 回だけ掃除する。
try { localStorage.removeItem('cpc.lastSeenLen') } catch { /* storage 無効環境は無視 */ }

function loadUnreadDone() {
  const parsed = lsGet(LS_UNREAD_DONE)
  return parsed && typeof parsed === 'object' ? parsed : {}
}

export function useSessionBadges({ sids, activeSid, messages, loading }) {
  // sid → true なら「turn 完了して未閲覧」
  const [unreadDone, setUnreadDone] = useState(loadUnreadDone)
  // 前回 render 時の loading[sid]。 true→false 遷移検出用。
  const prevLoadingRef = useRef({})

  // messages の最新 ref (= pending question 判定用)
  const messagesRef = useRef(messages)
  useEffect(() => { messagesRef.current = messages }, [messages])

  // localStorage 永続化
  useEffect(() => {
    lsSet(LS_UNREAD_DONE, unreadDone)
  }, [unreadDone])

  // 明示的既読化: session click 時に呼ばれる。 activeSid useEffect の前に
  // sync で赤丸を落とせる経路。
  const markAsSeen = useCallback((sid) => {
    if (!sid) return
    setUnreadDone(prev => (prev[sid] ? { ...prev, [sid]: false } : prev))
  }, [])

  // loading[sid] が true→false に変化した sid を unreadDone=true でマーク。
  // 同時に active タブの sid は積み立てずスキップ (= 見ている最中の完了は赤化不要)。
  useEffect(() => {
    const prev = prevLoadingRef.current
    const next = {}
    let mutated = false
    const flips = []
    for (const sid of sids) {
      const wasLoading = !!prev[sid]
      const isLoading = !!loading[sid]
      next[sid] = isLoading
      if (wasLoading && !isLoading && sid !== activeSid) {
        flips.push(sid)
      }
    }
    prevLoadingRef.current = next
    if (flips.length === 0) return
    setUnreadDone(p => {
      const out = { ...p }
      for (const sid of flips) {
        if (!out[sid]) { out[sid] = true; mutated = true }
      }
      return mutated ? out : p
    })
  }, [sids, loading, activeSid])

  // active タブに切替 / active タブの状態が動いた時に赤丸を落とす。
  useEffect(() => {
    if (!activeSid) return
    setUnreadDone(prev => (prev[activeSid] ? { ...prev, [activeSid]: false } : prev))
  }, [activeSid])

  // 削除された session のエントリ掃除。
  useEffect(() => {
    setUnreadDone(prev => {
      const sidSet = new Set(sids)
      const next = { ...prev }
      let changed = false
      for (const k of Object.keys(next)) {
        if (!sidSet.has(k)) { delete next[k]; changed = true }
      }
      return changed ? next : prev
    })
  }, [sids])

  // 表示状態 signature: pending question 有無 + loading 状態 + unreadDone。
  const sessionStateSig = useMemo(
    () => sids.map(sid => {
      const arr = messages[sid] || []
      const pending = arr.some(m => m.askUserQuestion && !m.askUserQuestion.answered)
      return `${sid}:${pending ? 'p' : ''}:${loading[sid] ? 'l' : ''}:${unreadDone[sid] ? 'n' : ''}`
    }).join('|'),
    [sids, messages, loading, unreadDone]
  )

  // sessionBadges / unreadCount: signature が同じ間は同じ object を返す。
  // unreadCount はアプリバッジ数字 = 赤丸が立った session 数。
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
      if (unreadDone[sid]) { badges[sid] = { kind: 'new', label: '●' }; count++; continue }
      badges[sid] = null
    }
    return { sessionBadges: badges, unreadCount: count }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSid, sessionStateSig])
  return { sessionBadges, unreadCount, markAsSeen }
}


