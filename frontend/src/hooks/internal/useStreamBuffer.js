import { useRef } from 'react'
import { generateId } from '../../utils/id.js'

// SSE で飛んでくる細切れの assistant 更新 (text / thinking / tool_use) を、
// rAF で 1 フレームに 1 回だけ React state にコミットするためのバッファ。
// SDK は数十 ms 周期で更新を投げるので、setState を毎回呼ぶと再描画が詰まる。
//
// セッションごとに独立した buffer を持つ。 セッション (= session_id) は動的に
// 増減するので、 `bufFor(sid)` で lazy 初期化する。
//
// 公開する ref:
// - streamBufRef                 : 受信中の最新スナップショット (session_id → buf)
//
// 公開関数:
// - flushStreamBuf(sid)        : バッファを setState に反映
// - scheduleFlush(sid)         : rAF で 1 回だけ flush を予約
// - cancelAndFlush(sid)        : 予約をキャンセルして即 flush
// - resetBuf(sid)              : 新規ターン / reconnect 開始時の初期化
function emptyBuf() {
  return { text: null, thinking: null, newTools: [], needsNewBubble: false, uuid: null, dirty: false }
}

export function useStreamBuffer({ setMessages }) {
  const streamBufRef = useRef({})
  const rafIdRef = useRef({})

  const bufFor = (sid) => {
    if (!streamBufRef.current[sid]) streamBufRef.current[sid] = emptyBuf()
    return streamBufRef.current[sid]
  }

  const flushStreamBuf = (sid) => {
    const buf = streamBufRef.current[sid]
    if (!buf || !buf.dirty) return

    const snap = {
      text: buf.text,
      thinking: buf.thinking,
      newTools: [...buf.newTools],
      needsNewBubble: buf.needsNewBubble,
      uuid: buf.uuid,
    }
    buf.text = null
    buf.thinking = null
    buf.newTools = []
    buf.needsNewBubble = false
    buf.uuid = null
    buf.dirty = false

    setMessages(prev => {
      const cur = prev[sid] || []
      const msgs = [...cur]
      const last = msgs[msgs.length - 1]
      const lastIsEmptyAgent = last
        && last.role === 'agent'
        && last.streaming
        && !last.text
        && !last.thinking
        && (!last.tools || last.tools.length === 0)
        && !last.askUserQuestion

      if (snap.needsNewBubble) {
        // 同 uuid (= Anthropic message.id) の追加 frame と reconnect / replay 時の
        // 二重到着を兼用で吸収する。 JSONL は 1 つの assistant message を複数行に分けて
        // partial で書く (= tool_use を別行で追記する等) ので、 後から来たフレームの
        // content (= 新規 tool_use) を**既存 bubble に追記マージ**する。
        // 上書きでなくマージなのが重要: 旧実装は tools = [...snap.newTools] で
        // 既存 tool を消してた → multi-frame の 2 個目で 1 個目が消える bug。
        if (snap.uuid) {
          const existIdx = msgs.findIndex(m => m.uuid === snap.uuid)
          if (existIdx >= 0) {
            const existing = msgs[existIdx]
            const existingTools = existing.tools || []
            const existingIds = new Set(existingTools.map(t => t.id))
            const addedTools = (snap.newTools || []).filter(t => !existingIds.has(t.id))
            msgs[existIdx] = {
              ...existing,
              // text / thinking は frame ごとに完全形で来るので、 非空なら新値、 空なら既存維持
              text: snap.text || existing.text || '',
              thinking: snap.thinking || existing.thinking || null,
              tools: addedTools.length > 0 ? [...existingTools, ...addedTools] : existingTools,
              streaming: existing.streaming,
            }
            return { ...prev, [sid]: msgs }
          }
        }
        // AssistantMessage 単位で 1 bubble。送信直後の空 streaming placeholder が
        // あればそこに今回の中身を埋めて推論中表示を消す。
        if (lastIsEmptyAgent) {
          msgs[msgs.length - 1] = {
            ...last,
            uuid: snap.uuid || last.uuid,
            text: snap.text || '',
            thinking: snap.thinking || null,
            tools: [...(snap.newTools || [])],
          }
          return { ...prev, [sid]: msgs }
        }
        return { ...prev, [sid]: [...msgs, {
          id: generateId(),
          uuid: snap.uuid || null,
          role: 'agent',
          text: snap.text || '',
          thinking: snap.thinking || null,
          tools: [...(snap.newTools || [])],
          streaming: true,
        }]}
      }

      // reconnect 再生など、既存バブルに積み増すパス
      if (!last || last.role !== 'agent') return prev
      const updated = { ...last }
      if (snap.text !== null) updated.text = snap.text
      if (snap.thinking !== null) updated.thinking = snap.thinking
      if (snap.newTools.length > 0) {
        const existing = updated.tools || []
        const existingIds = new Set(existing.map(t => t.id))
        const toAdd = snap.newTools.filter(t => !existingIds.has(t.id))
        if (toAdd.length > 0) updated.tools = [...existing, ...toAdd]
      }
      msgs[msgs.length - 1] = updated
      return { ...prev, [sid]: msgs }
    })
  }

  const scheduleFlush = (sid) => {
    if (rafIdRef.current[sid] != null) return
    rafIdRef.current[sid] = requestAnimationFrame(() => {
      rafIdRef.current[sid] = null
      flushStreamBuf(sid)
    })
  }

  const cancelAndFlush = (sid) => {
    if (rafIdRef.current[sid] != null) {
      cancelAnimationFrame(rafIdRef.current[sid])
      rafIdRef.current[sid] = null
    }
    flushStreamBuf(sid)
  }

  const resetBuf = (sid) => {
    streamBufRef.current[sid] = emptyBuf()
  }

  return {
    streamBufRef,
    flushStreamBuf,
    scheduleFlush,
    cancelAndFlush,
    resetBuf,
    bufFor,
  }
}
