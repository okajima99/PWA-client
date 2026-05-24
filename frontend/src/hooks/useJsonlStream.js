import { useState, useRef, useEffect } from 'react'
import { API_BASE, MAX_MESSAGES } from '../constants.js'
import { generateId } from '../utils/id.js'
import { useStreamBuffer } from './internal/useStreamBuffer.js'
import { processStreamEvent } from './internal/processStreamEvent.js'

// claude の JSONL を tail する backend SSE (= /jsonl/stream/{sid}) を購読し、
// 旧 chat UI と同じ message state を組み立てる出力専用フック。 入力 (= キー送信) は
// 別経路 (= PTY WebSocket) なので、 ここは受信・表示だけを担う。
//
// 既存資産の再利用:
//   - useStreamBuffer  : rAF で 1 フレーム 1 commit に coalesce + AssistantMessage uuid dedup
//   - processStreamEvent: assistant / user(tool_result) / result / ask_user_question を解釈
// JSONL 固有の user_message (= ユーザ発言) だけ processStreamEvent に無いのでここで処理する。
//
// session (= タブ) 切替で SSE を張り直す。 再接続時は backend が先頭から replay するが、
// uuid dedup で二重表示にならない (= 冪等)。
export function useJsonlStream({ activeSession }) {
  const sid = activeSession?.id
  const [messages, setMessages] = useState({})
  const [apiKeySource, setApiKeySource] = useState({})
  // 推論中フラグ (= 送信/停止ボタンのトグル用)。 assistant 受信で true、
  // turn 完了 (= result event, stop_reason=end_turn) で false。
  const [streaming, setStreaming] = useState({})

  const buffer = useStreamBuffer({ setMessages })

  // processStreamEvent に渡す deps。 JSONL 経路では request_id / result の loading 制御は
  // 不要なので no-op を渡す (= 入力経路を持たないため loading state を管理しない)。
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

  // handleEvent を ref に逃がして、 SSE は sid 変更時だけ張り直す (= 毎 render の
  // closure 更新で再接続しない)。
  // ref 更新は render 中でなく effect で行う (= react-hooks/refs ルール)。
  const handleEventRef = useRef(null)
  useEffect(() => {
    handleEventRef.current = (curSid, event) => {
      if (event.type === 'user_message') {
        // 進行中の agent buffer を確定してから user バブルを足す
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
        setStreaming(prev => (prev[curSid] ? prev : { ...prev, [curSid]: true }))
      } else if (event.type === 'result') {
        setStreaming(prev => (prev[curSid] === false ? prev : { ...prev, [curSid]: false }))
      }
      try {
        processStreamEvent(eventDeps, curSid, event)
      } catch { /* 1 event の失敗で stream 全体を落とさない */ }
    }
  })

  useEffect(() => {
    if (!sid) return undefined

    buffer.resetBuf(sid)
    // 張り直し時は一旦クリアして全 replay で再構築 (= 二重防止は uuid dedup が担うが、
    // session 切替時に前の内容が残らないよう明示的に空に戻す)
    setMessages(prev => ({ ...prev, [sid]: [] }))

    const es = new EventSource(`${API_BASE}/jsonl/stream/${encodeURIComponent(sid)}`)
    es.onmessage = (e) => {
      if (!e.data) return
      let event
      try {
        event = JSON.parse(e.data)
      } catch {
        return
      }
      handleEventRef.current?.(sid, event)
    }
    es.onerror = () => {
      // EventSource はネットワーク回復時に自動再接続する。 明示的な処理は不要。
    }

    return () => {
      es.close()
      buffer.cancelAndFlush(sid)
    }
  }, [sid]) // eslint-disable-line react-hooks/exhaustive-deps

  return { messages, apiKeySource, streaming }
}
