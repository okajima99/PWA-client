import { useState, useRef, useEffect, useCallback } from 'react'
import { LS_JSONL_OFFSET } from '../constants.js'
import { apiFetch, apiUrl } from '../utils/api.js'
import { lsGet, lsSet } from '../utils/storage.js'
import { generateId } from '../utils/id.js'
import { useStreamBuffer } from './internal/useStreamBuffer.js'
import { processStreamEvent } from './internal/processStreamEvent.js'
import { reconcileUserMessage } from './internal/reconcileUserMessage.js'

// session_id → JSONL byte offset の永続化。 タブ切替 / リロードを跨いで「ここまで読んだ」 を
// 保持し、 新規 EventSource 接続時に `?from=<offset>` で渡す。 backend は offset 以降の
// 完全行だけ流すので、 初回 replay の重さがほぼゼロになる。
function loadOffsets() {
  const parsed = lsGet(LS_JSONL_OFFSET)
  return parsed && typeof parsed === 'object' ? parsed : {}
}

function persistOffsets(offsets) {
  lsSet(LS_JSONL_OFFSET, offsets)
}

// chat 1 セッションの送受信・状態管理を束ねる公開フック (= TUI / JSONL 版)。
//
// 旧 SDK + proxy 版を置き換えたもの。 App.jsx 側のインターフェース
// (loading / sendMessage / stopMessage / apiKeySource / sendAnswer / fetchLatest /
//  endSession / setLoading / pendingSendUntilRef) は維持し、 App.jsx はほぼ無改修で動く。
//
// 受信: 常時 /jsonl/stream を EventSource で購読 (= claude が書く JSONL を backend が tail)。
//       event は processStreamEvent + useStreamBuffer で旧 chat と同じ message state に組む。
// 送信: POST /pty/{sid}/send (= tmux send-keys、 text+Enter / Escape)。
// 表示資産 (MessageItem / scroll / localStorage) は App.jsx 側のものをそのまま使う。
export function useChatStream({
  activeSession,
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
  // EventSource 再接続トリガ。 endSession (/clear) で新 claude_sid に切り替わるとき、
  // backend の JSONL 解決を新 sid に向けるためここを +1 して useEffect を再実行させる。
  const [reconnectKey, setReconnectKey] = useState(0)

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
          const next = reconcileUserMessage(cur, event.text || '', event.uuid)
          return next === cur ? prev : { ...prev, [curSid]: next }
        })
        return
      }
      if (event.type === 'assistant') {
        setLoading(prev => (prev[curSid] ? prev : { ...prev, [curSid]: true }))
      } else if (event.type === 'result') {
        // result = turn 完了でのみ loading 解放。 ask_user_question では解放しない:
        // AskUserQuestion 中の停止ボタンは status.pending_question が担い、 回答後の
        // JSONL flush で ask_user_question event が再発火しても loading を落とさない
        // (= 回答直後に送信ボタンへ戻って誤送信できてしまう問題を防ぐ)。
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
      ? apiUrl(`/jsonl/stream/${encodeURIComponent(sid)}?from=${encodeURIComponent(from)}`)
      : apiUrl(`/jsonl/stream/${encodeURIComponent(sid)}`)
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
  }, [sid, reconnectKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // chat UI の操作 → tmux session にキー送信 (= 出力 SSE と分離)。
  const sendToPty = useCallback(async (targetSid, body) => {
    try {
      await apiFetch(`/pty/${encodeURIComponent(targetSid)}/send`, {
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
      const imageRefs = files.filter(f => f.imageId).map(f => f.imageId)
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
            // imageUrls = ObjectURL (= 一時表示用、 リロードで失効)、
            // imageRefs = IndexedDB key (= 永続、 リロード後 AttachedImages が復元)
            imageUrls: imageUrls.length > 0 ? imageUrls : undefined,
            imageRefs: imageRefs.length > 0 ? imageRefs : undefined,
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
        await apiFetch(`/pty/${encodeURIComponent(sid)}/send-with-files`, {
          method: 'POST',
          body: form,
        })
      } catch { /* 送信失敗は握りつぶす、 次操作で復帰 */ }
      clearAttachments(sid)
    } else {
      // 送信本文 (text + Enter): backend が JSONL に user 行が +1 されるかを最大 2s 監視 →
      // なければ 1 回自動再送 → さらに 1.5s 待つ → ok/ng を返す。 ng (= claude TUI に届かなかった)
      // 時は input に text を戻して再送可能にし、 楽観 user bubble に「届かなかった」 マークを付ける。
      let result
      try {
        const r = await apiFetch(`/pty/${encodeURIComponent(sid)}/send`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, enter: true }),
        })
        result = r ? await r.json().catch(() => ({ ok: false })) : { ok: false }
      } catch {
        result = { ok: false }
      }
      if (!result.ok) {
        // input に text を戻す (= ユーザが再入力済みなら prev を尊重)
        setInput(prev => ({ ...prev, [sid]: prev[sid] || text }))
        setMessages(prev => {
          const msgs = [...(prev[sid] || [])]
          // 末尾の空 streaming agent bubble を撤去 (= 推論されてないので)
          while (msgs.length) {
            const tail = msgs[msgs.length - 1]
            if (tail.role === 'agent' && tail.streaming && !tail.text && !tail.thinking && (!tail.tools || !tail.tools.length)) {
              msgs.pop()
            } else break
          }
          // 末尾の楽観 user bubble に sendFailed: true を付ける (= MessageItem が ⚠ を出す)
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role === 'user' && msgs[i].optimistic) {
              msgs[i] = { ...msgs[i], sendFailed: true }
              break
            }
          }
          return { ...prev, [sid]: msgs }
        })
        setLoading(prev => ({ ...prev, [sid]: false }))
        pendingSendUntilRef.current[sid] = 0
      }
    }
  }, [sid, input, attachments, loading, setInput, setMessages, clearAttachments, scrollToBottom, sendToPty, isAtBottomRef, setLoading])

  const stopMessage = useCallback(async () => {
    if (!sid) return
    await sendToPty(sid, { key: 'Escape' })
    setLoading(prev => ({ ...prev, [sid]: false }))
    pendingSendUntilRef.current[sid] = 0
  }, [sid, sendToPty])

  const sendAnswer = useCallback(async (targetSid, tool_use_id, answer, isFree = false, optionCount = 0) => {
    // AskUserQuestion の回答を tmux 経由で claude TUI に送る。
    // 回答 = turn 再開の合図なので、 送信 (sendMessage) と同じく loading を立てて
    // 送信ボタン → 停止ボタンに切り替える (= 楽観的に pendingSend deadline も置く)。
    setLoading(prev => ({ ...prev, [targetSid]: true }))
    pendingSendUntilRef.current[targetSid] = Date.now() + 1500
    if (isFree) {
      // 自由記述: claude TUI は選択肢リストの末尾に "Type something"(自由入力) を持つ。
      // フォーカスは先頭選択肢にあるので、 素のテキストを送ると先頭が選ばれてしまう
      // (= 自由記述が届かない原因)。 先に "Type something"(= 選択肢数+1 番) を選んで
      // 自由入力モードに入れてから、 テキスト + Enter を送る。
      const typeNum = String((optionCount || 0) + 1)
      await sendToPty(targetSid, { text: typeNum, enter: false })
      await new Promise(r => setTimeout(r, 150))
      await sendToPty(targetSid, { text: answer, enter: true })
    } else {
      await sendToPty(targetSid, { text: answer, enter: true })
    }
    setMessages(prev => {
      const cur = prev[targetSid] || []
      const msgs = cur.map(m =>
        m.askUserQuestion?.tool_use_id === tool_use_id
          ? { ...m, askUserQuestion: { ...m.askUserQuestion, answered: true, selectedAnswer: answer } }
          : m,
      )
      return { ...prev, [targetSid]: msgs }
    })
  }, [sendToPty, setMessages, setLoading])

  const endSession = useCallback(async () => {
    if (!sid) return
    // セッション終了 = claude プロセスを kill + 新規 spawn する (= /clear と違って
    // プロセスメモリも完全解放、 ターミナル描画の重さ / CPU 高負荷の根本対策)。
    // 新 claude_sid に切り替わるが backend の SessionStart hook で bindings が更新されるので
    // PWA タブはそのまま続けて使える。 旧 JSONL は disk に残るので --resume で復元可能。
    try {
      await apiFetch(`/sessions/${encodeURIComponent(sid)}/restart`, { method: 'POST' })
    } catch { /* 失敗しても次操作で復帰 */ }
    // UI 上のセッション区切りを messages に挿入 (= MessageItem の system/kind=session_end 経路)
    setMessages(prev => ({
      ...prev,
      [sid]: [
        ...(prev[sid] || []),
        { id: generateId(), role: 'system', kind: 'session_end', ts: Date.now() },
      ],
    }))
    // 旧 JSONL を読み続けないよう offset をクリア (= 新 claude_sid に切り替わったら新 JSONL の
    // 末尾近くから tail 開始させる)。 SessionStart hook で binding 更新まで少し待つ。
    delete offsetRef.current[sid]
    persistOffsets(offsetRef.current)
    setTimeout(() => {
      setReconnectKey(k => k + 1)
    }, 2000)
  }, [sid, setMessages])

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
