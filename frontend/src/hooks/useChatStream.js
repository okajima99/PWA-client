import { useState, useRef, useEffect, useCallback } from 'react'
import { API_BASE, LS_JSONL_OFFSET, MAX_MESSAGES } from '../constants.js'
import { generateId } from '../utils/id.js'
import { useStreamBuffer } from './internal/useStreamBuffer.js'
import { processStreamEvent } from './internal/processStreamEvent.js'

// session_id → JSONL byte offset の永続化。 タブ切替 / リロードを跨いで「ここまで読んだ」 を
// 保持し、 新規 EventSource 接続時に `?from=<offset>` で渡す。 backend は offset 以降の
// 完全行だけ流すので、 初回 replay の重さがほぼゼロになる。
function loadOffsets() {
  try {
    const raw = localStorage.getItem(LS_JSONL_OFFSET)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') return parsed
    }
  } catch { /* ignore */ }
  return {}
}

function persistOffsets(offsets) {
  try {
    localStorage.setItem(LS_JSONL_OFFSET, JSON.stringify(offsets))
  } catch { /* ignore */ }
}

// chat 1 セッションの送受信・状態管理を束ねる公開フック (= TUI / JSONL 版)。
//
// 旧 SDK + proxy 版を置き換えたもの。 App.jsx 側のインターフェース
// (loading / sendMessage / stopMessage / apiKeySource / sendAnswer / fetchLatest /
//  endSession / setLoading / pendingSendUntilRef / visibilitySuppressUntilRef) は維持し、
// App.jsx はほぼ無改修で動く。
//
// 受信: 常時 /jsonl/stream を EventSource で購読 (= claude が書く JSONL を backend が tail)。
//       event は processStreamEvent + useStreamBuffer で旧 chat と同じ message state に組む。
// 送信: POST /pty/{sid}/send (= tmux send-keys、 text+Enter / Escape)。
// 表示資産 (MessageItem / scroll / localStorage) は App.jsx 側のものをそのまま使う。
export function useChatStream({
  activeSession,
  sessions, // eslint-disable-line no-unused-vars
  setMessages,
  input, setInput,
  attachments, clearAttachments,
  scrollToBottom, isAtBottomRef,
}) {
  const sid = activeSession?.id || null
  const [loading, setLoading] = useState({})
  const [apiKeySource, setApiKeySource] = useState({})
  // App.jsx の showStopButton が参照する楽観 deadline。
  const pendingSendUntilRef = useRef({})
  // session ごとの最後に受信した byte offset。 タブ切替で再接続する時、 ここから差分だけ
  // 取り直すことで全 replay を避ける (= 切替を軽く + localStorage 即復元と併用)。
  // localStorage に永続化することで、 アプリ再起動 / リロードを跨いでも継続。
  const offsetRef = useRef(loadOffsets())
  const offsetPersistTimerRef = useRef(null)

  const buffer = useStreamBuffer({ setMessages })

  const eventDeps = {
    setMessages,
    setApiKeySource,
    cancelAndFlush: buffer.cancelAndFlush,
    scheduleFlush: buffer.scheduleFlush,
    streamBufRef: buffer.streamBufRef,
    bufFor: buffer.bufFor,
    onUserRequestId: () => {},
    onResultMessage: () => {},
  }

  // event ハンドラを ref に逃がして、 EventSource は sid 変更時だけ張り直す。
  // ref 更新は render 中でなく effect で行う (= react-hooks/refs ルール)。
  const handleEventRef = useRef(null)
  useEffect(() => {
    handleEventRef.current = (curSid, event) => {
      if (event.type === 'user_message') {
        buffer.cancelAndFlush(curSid)
        setMessages(prev => {
          const cur = prev[curSid] || []
          // 既知 uuid なら何もしない
          if (event.uuid && cur.some(m => m.role === 'user' && m.uuid === event.uuid)) {
            return prev
          }
          const eventText = event.text || ''
          // 添付付き送信の dedup: backend が「<本文> [添付ファイル: /path/...]」 を tmux に送る
          // → JSONL に同じ text で user 行が書かれる。 楽観 bubble (= fileNames を持つ
          // optimistic な user) と置換し、 path 込み全文は表示しない (= 元 text + 画像チップ
          // だけが残る、 二重表示を防ぐ)。
          if (eventText.includes('[添付ファイル: ')) {
            const optimIdx = cur.findIndex(
              m => m.role === 'user' && m.optimistic && (m.fileNames?.length || m.imageUrls?.length)
            )
            if (optimIdx >= 0) {
              const next = [...cur]
              next[optimIdx] = { ...next[optimIdx], uuid: event.uuid || null, optimistic: false }
              return { ...prev, [curSid]: next }
            }
          }
          // sendMessage が挿入した optimistic user bubble (uuid 無し、 同 text) と
          // 同一発話なら、 uuid を補完して optimistic フラグを外す (= 二重表示防止)。
          const optimIdx = cur.findIndex(
            m => m.role === 'user' && m.optimistic && m.text === eventText
          )
          if (optimIdx >= 0) {
            const next = [...cur]
            next[optimIdx] = { ...next[optimIdx], uuid: event.uuid || null, optimistic: false }
            return { ...prev, [curSid]: next }
          }
          return {
            ...prev,
            [curSid]: [
              ...cur,
              { id: generateId(), uuid: event.uuid || null, role: 'user', text: eventText },
            ].slice(-MAX_MESSAGES),
          }
        })
        return
      }
      if (event.type === 'assistant') {
        setLoading(prev => (prev[curSid] ? prev : { ...prev, [curSid]: true }))
      } else if (event.type === 'result') {
        setLoading(prev => (prev[curSid] === false ? prev : { ...prev, [curSid]: false }))
      }
      try {
        processStreamEvent(eventDeps, curSid, event)
      } catch { /* 1 event の失敗で stream を落とさない */ }
    }
  })

  useEffect(() => {
    if (!sid) return undefined
    buffer.resetBuf(sid)
    const from = offsetRef.current[sid]
    const url = from != null
      ? `${API_BASE}/jsonl/stream/${encodeURIComponent(sid)}?from=${encodeURIComponent(from)}`
      : `${API_BASE}/jsonl/stream/${encodeURIComponent(sid)}`
    const es = new EventSource(url)
    es.onmessage = (e) => {
      if (e.lastEventId) {
        offsetRef.current[sid] = e.lastEventId
        // 連続イベントで毎回 localStorage write すると重いので 1s debounce。
        if (offsetPersistTimerRef.current) clearTimeout(offsetPersistTimerRef.current)
        offsetPersistTimerRef.current = setTimeout(() => {
          offsetPersistTimerRef.current = null
          persistOffsets(offsetRef.current)
        }, 1000)
      }
      if (!e.data) return
      let event
      try {
        event = JSON.parse(e.data)
      } catch {
        return
      }
      handleEventRef.current?.(sid, event)
    }
    es.onerror = () => { /* EventSource は自動再接続 (= Last-Event-ID で差分) */ }
    return () => {
      es.close()
      buffer.cancelAndFlush(sid)
    }
  }, [sid]) // eslint-disable-line react-hooks/exhaustive-deps

  // chat UI の操作 → tmux session にキー送信 (= 出力 SSE と分離)。
  const sendToPty = useCallback(async (targetSid, body) => {
    try {
      await fetch(`${API_BASE}/pty/${encodeURIComponent(targetSid)}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch { /* 送信失敗は握りつぶす (= 次操作で復帰) */ }
  }, [])

  const sendMessage = useCallback(async () => {
    if (!sid) return
    const text = (input[sid] || '').trim()
    const files = attachments[sid] || []
    if ((!text && files.length === 0) || loading[sid]) return
    setInput(prev => ({ ...prev, [sid]: '' }))
    setLoading(prev => ({ ...prev, [sid]: true }))
    pendingSendUntilRef.current[sid] = Date.now() + 1500
    // 楽観 user bubble + 空 streaming agent bubble を即挿入。 添付があれば user bubble に
    // 表示用の imageUrls / fileNames を載せる (= MessageItem の user-block 経路で render)。
    // imageUrls は ObjectURL なのでアプリリロード後は消えるが、 当該セッション中は見える。
    setMessages(prev => {
      const cur = prev[sid] || []
      const imageUrls = files.filter(f => f.url).map(f => f.url)
      const fileNames = files.map(f => f.file.name)
      return {
        ...prev,
        [sid]: [
          ...cur,
          {
            id: generateId(),
            role: 'user',
            text,
            optimistic: true,
            imageUrls: imageUrls.length > 0 ? imageUrls : undefined,
            fileNames: fileNames.length > 0 ? fileNames : undefined,
          },
          { id: generateId(), role: 'agent', text: '', tools: [], streaming: true },
        ],
      }
    })
    if (isAtBottomRef) isAtBottomRef.current = true
    scrollToBottom()
    if (files.length > 0) {
      // multipart: backend がファイルを uploads/tmp に保存して path を本文に追記して
      // tmux に送る (= claude が Read tool で読む)。
      const form = new FormData()
      form.append('text', text)
      for (const item of files) {
        form.append('files', item.file)
      }
      try {
        await fetch(`${API_BASE}/pty/${encodeURIComponent(sid)}/send-with-files`, {
          method: 'POST',
          body: form,
        })
      } catch { /* 送信失敗は握りつぶす、 次操作で復帰 */ }
      clearAttachments(sid)
    } else {
      await sendToPty(sid, { text, enter: true })
    }
  }, [sid, input, attachments, loading, setInput, setMessages, clearAttachments, scrollToBottom, sendToPty, isAtBottomRef])

  const stopMessage = useCallback(async () => {
    if (!sid) return
    await sendToPty(sid, { key: 'Escape' })
    setLoading(prev => ({ ...prev, [sid]: false }))
    pendingSendUntilRef.current[sid] = 0
  }, [sid, sendToPty])

  const sendAnswer = useCallback(async (targetSid, tool_use_id, answer) => {
    // AskUserQuestion の回答を tmux に流す (= MVP は answer テキスト + Enter)。
    await sendToPty(targetSid, { text: answer, enter: true })
    setMessages(prev => {
      const cur = prev[targetSid] || []
      const msgs = cur.map(m =>
        m.askUserQuestion?.tool_use_id === tool_use_id
          ? { ...m, askUserQuestion: { ...m.askUserQuestion, answered: true, selectedAnswer: answer } }
          : m,
      )
      return { ...prev, [targetSid]: msgs }
    })
  }, [sendToPty, setMessages])

  const endSession = useCallback(async () => {
    if (!sid) return
    // `/clear` で claude TUI の context をリセットする (= 新 claude_sid が振られて新 JSONL に
    // 切り替わる、 旧会話の JSONL は ~/.claude/projects/ にファイルとして永続)。 claude 自体は
    // 同じ tmux プロセスで動き続けるので起動エイリアス再入力は不要、 即時。
    await sendToPty(sid, { text: '/clear', enter: true })
  }, [sid, sendToPty])

  // 常時 tail + EventSource 自動再接続なので明示 fetch は不要。 scroll だけ最新へ寄せる。
  const fetchLatest = useCallback(() => {
    scrollToBottom()
  }, [scrollToBottom])

  return {
    loading,
    setLoading,
    apiKeySource,
    sendMessage,
    sendAnswer,
    stopMessage,
    fetchLatest,
    endSession,
    pendingSendUntilRef,
  }
}
