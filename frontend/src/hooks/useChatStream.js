import { useState, useRef, useEffect, useCallback } from 'react'
import { API_BASE, MAX_MESSAGES } from '../constants.js'
import { generateId } from '../utils/id.js'
import { useStreamBuffer } from './internal/useStreamBuffer.js'
import { processStreamEvent } from './internal/processStreamEvent.js'

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
  attachments, clearAttachments, // eslint-disable-line no-unused-vars
  scrollToBottom, isAtBottomRef,
}) {
  const sid = activeSession?.id || null
  const [loading, setLoading] = useState({})
  const [apiKeySource, setApiKeySource] = useState({})
  // App.jsx の showStopButton が参照する楽観 deadline / visibility 抑止 (= インターフェース維持)。
  const pendingSendUntilRef = useRef({})
  const visibilitySuppressUntilRef = useRef(0)
  // session ごとの最後に受信した byte offset。 タブ切替で再接続する時、 ここから差分だけ
  // 取り直すことで全 replay を避ける (= 切替を軽く + localStorage 即復元と併用)。
  const offsetRef = useRef({})

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
          if (event.uuid && cur.some(m => m.role === 'user' && m.uuid === event.uuid)) {
            return prev
          }
          return {
            ...prev,
            [curSid]: [
              ...cur,
              { id: generateId(), uuid: event.uuid || null, role: 'user', text: event.text || '' },
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
      if (e.lastEventId) offsetRef.current[sid] = e.lastEventId
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
    if (!text || loading[sid]) return
    setInput(prev => ({ ...prev, [sid]: '' }))
    setLoading(prev => ({ ...prev, [sid]: true }))
    // backend が assistant を JSONL に書くまでの間、 楽観的に停止ボタンを出す。
    pendingSendUntilRef.current[sid] = Date.now() + 1500
    if (isAtBottomRef) isAtBottomRef.current = true
    scrollToBottom()
    await sendToPty(sid, { text, enter: true })
  }, [sid, input, loading, setInput, scrollToBottom, sendToPty, isAtBottomRef])

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
    await sendToPty(sid, { text: '/exit', enter: true })
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
    visibilitySuppressUntilRef,
    pendingSendUntilRef,
  }
}
