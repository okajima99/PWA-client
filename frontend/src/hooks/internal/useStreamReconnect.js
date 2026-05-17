import { useEffect, useRef } from 'react'
import { API_BASE, MAX_MESSAGES } from '../../constants.js'
import { generateId } from '../../utils/id.js'
import { nextNextFrame } from '../../utils/raf.js'
import { processStreamEvent } from './processStreamEvent.js'

// SSE が切れた後の復帰を担当する。
// - reconnectStream(sid)        : サーバ側 buffer を先頭から再生
// - reconnectIfStreaming(sid)   : サーバ status を見て streaming 中なら reconnect
// - checkAndReconnect(force)    : 起動時 / ネット復帰時の状態問い合わせ
// - fetchLatest()               : 「最新を取得」ボタン用
// - forceResyncAll()            : visibility/pageshow 復帰時の強制再同期
//
// セッションは動的なので、 セッションリスト (`sessionsRef.current`) から都度取り出す。
export function useStreamReconnect({
  setMessages,
  setLoading,
  setApiKeySource,
  buffer,
  scrollToBottom,
  isAtBottomRef,
  loadingRef,
  abortControllers,
  activeSessionRef,
  sessionsRef,
  onUserRequestId,
  onResultMessage,
}) {
  const reconnectingRef = useRef({})
  // (A4 fix) reconnect setTimeout の id を保持して unmount cleanup で clear する。
  // unmount 中に発火すると state 更新 warning + 二重接続。
  const reconnectTimerRef = useRef(null)
  // visibility 復帰直後の N ms は外部 watcher (= App.jsx の buffer_length watcher) も
  // reconnect 発火を控えてほしい。 visibilitychange リスナでここに deadline を書き込み、
  // expose して watcher 側で参照する。 visibility 経路と buffer 経路の重複 reconnect race
  // を防ぐ「単一の真実」 として hook 内に置く。
  const visibilitySuppressUntilRef = useRef(0)

  const eventDeps = {
    setMessages,
    setApiKeySource,
    cancelAndFlush: buffer.cancelAndFlush,
    scheduleFlush: buffer.scheduleFlush,
    streamBufRef: buffer.streamBufRef,
    bufFor: buffer.bufFor,
    onUserRequestId,
    onResultMessage,
  }

  const allSessionIds = () => (sessionsRef.current || []).map(s => s.id)

  // --- reconnectStream の内部 helper 群 (= 90 行関数を責務別に分割) ---

  // 1. fetch を投げて 204 / エラーをハンドル、 ストリームの response を返す。
  //    cache-bust + cache:'no-store' は iOS Safari の GET キャッシュ回避対策。
  const _openReplayFetch = async (sid) => {
    const controller = new AbortController()
    abortControllers.current[sid] = controller
    const url = `${API_BASE}/chat/${sid}/reconnect?from=0&_t=${Date.now()}`
    const res = await fetch(url, { cache: 'no-store', signal: controller.signal })
    if (res.status === 204 || !res.ok) return null
    return res
  }

  // 2. backend の現在 streaming 状態を取得 (= reconnect 中の loading 制御に使う)。
  //    flicker 防止のため、 backend が streaming=true でない時は loading を触らない。
  const _checkStreamingNow = async (sid) => {
    try {
      const s = await fetch(`${API_BASE}/status/${sid}`).then(r => r.ok ? r.json() : null)
      return !!s?.streaming
    } catch { return false }
  }

  // 3. replay 開始時の messages 整形: 末尾の error bubble を取り除き、 末尾 agent bubble を
  //    空 streaming に初期化、 無ければ新規 streaming bubble を追加。
  const _seedMessagesForReplay = (sid) => {
    setMessages(prev => {
      const cur = prev[sid] || []
      let trimmed = [...cur]
      while (trimmed.length > 0 && trimmed[trimmed.length - 1].role === 'error') {
        trimmed.pop()
      }
      const last = trimmed[trimmed.length - 1]
      if (last?.role === 'agent') {
        const updated = [...trimmed]
        updated[updated.length - 1] = { ...last, text: '', tools: [], thinking: null, meta: undefined, streaming: true }
        return { ...prev, [sid]: updated }
      }
      return { ...prev, [sid]: [...trimmed, { id: generateId(), role: 'agent', text: '', tools: [], streaming: true }].slice(-MAX_MESSAGES) }
    })
  }

  // 4. SSE ストリームを最後まで読んで 1 line ずつ processStreamEvent に渡す。
  const _consumeReplayStream = async (sid, res) => {
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop()
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6).trim()
        if (!data) continue
        try {
          processStreamEvent(eventDeps, sid, JSON.parse(data))
        } catch { /* ignored */ }
      }
    }
  }

  // 5. replay 完了後の終了処理: buffer flush、 loading 戻し、 streaming flag 落とし、 scroll。
  const _teardownReplay = (sid, streamingNow) => {
    buffer.cancelAndFlush(sid)
    if (streamingNow) {
      setLoading(prev => ({ ...prev, [sid]: false }))
    }
    setMessages(prev => {
      const cur = prev[sid] || []
      const msgs = [...cur]
      if (msgs.length > 0 && msgs[msgs.length - 1].streaming) {
        msgs[msgs.length - 1] = { ...msgs[msgs.length - 1], streaming: false }
      }
      return { ...prev, [sid]: msgs }
    })
    requestAnimationFrame(scrollToBottom)
  }

  // reconnect: T1 移行で常に from=0 で全 buffer 再生する。
  //  - 204 なら false、 データあり (ストリーミング完了) なら true を返す
  //  - 内部処理は上の _openReplayFetch / _checkStreamingNow / _seedMessagesForReplay /
  //    _consumeReplayStream / _teardownReplay に分割
  //  - ストリーム完了時に backend が依然 streaming=true ならば 1 秒後に自動 re-reconnect
  const reconnectStream = async (sid) => {
    const res = await _openReplayFetch(sid)
    if (!res) return false

    isAtBottomRef.current = true
    const streamingNow = await _checkStreamingNow(sid)
    if (streamingNow) {
      setLoading(prev => ({ ...prev, [sid]: true }))
    }
    _seedMessagesForReplay(sid)
    buffer.resetBuf(sid)

    let needsReconnect = false
    try {
      await _consumeReplayStream(sid, res)
      needsReconnect = await _checkStreamingNow(sid)
      return true
    } finally {
      _teardownReplay(sid, streamingNow)
      if (needsReconnect) {
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null
          reconnectStream(sid)
        }, 1000)
      }
    }
  }

  // 「status を fetch → streaming/pending_question なら abort + reconnect」 の共通ロジック。
  // single-flight ガードを 1 箇所に集約: 既に reconnect 中なら abort で踏みつぶさない。
  // reconnectIfStreaming / checkAndReconnect / forceResyncAll はこの helper を呼ぶ薄ラッパー。
  const _maybeReconnect = async (sid, { setLoadingOnStreaming = false } = {}) => {
    if (reconnectingRef.current[sid]) return false
    let s = null
    try {
      s = await fetch(`${API_BASE}/status/${sid}`).then(r => r.json()).catch(() => null)
    } catch { /* ignored */ }
    if (!s) return false
    if (!(s.streaming || s.pending_question_tool_id)) return false
    // await を挟んだので二重ガード
    if (reconnectingRef.current[sid]) return false
    if (setLoadingOnStreaming && s.streaming) {
      setLoading(prev => ({ ...prev, [sid]: true }))
    }
    if (abortControllers.current[sid]) {
      abortControllers.current[sid].abort()
      abortControllers.current[sid] = null
    }
    reconnectingRef.current[sid] = true
    reconnectStream(sid).finally(() => {
      reconnectingRef.current[sid] = false
    })
    return true
  }

  const reconnectIfStreaming = (sid) => _maybeReconnect(sid)

  const checkAndReconnect = async (forceReconnect = false) => {
    for (const sid of allSessionIds()) {
      if (!forceReconnect && loadingRef.current[sid]) continue
      await _maybeReconnect(sid, { setLoadingOnStreaming: true })
    }
  }

  const fetchLatest = async () => {
    const active = activeSessionRef.current
    const sid = active?.id
    if (!sid || reconnectingRef.current[sid]) return

    if (abortControllers.current[sid]) {
      abortControllers.current[sid].abort()
      abortControllers.current[sid] = null
    }

    try {
      const s = await fetch(`${API_BASE}/status/${sid}`).then(r => r.json()).catch(() => null)
      if (s?.streaming) {
        setLoading(prev => ({ ...prev, [sid]: true }))
      }
    } catch { /* ignored */ }

    reconnectingRef.current[sid] = true
    try {
      const hadData = await reconnectStream(sid)
      if (!hadData) {
        const s = await fetch(`${API_BASE}/status/${sid}`).then(r => r.json()).catch(() => null)
        if (!s?.streaming) {
          setLoading(prev => ({ ...prev, [sid]: false }))
        }
      }
    } finally {
      reconnectingRef.current[sid] = false
    }
  }

  const forceResyncAll = () => {
    for (const sid of allSessionIds()) {
      if (reconnectingRef.current[sid]) continue
      if (abortControllers.current[sid]) {
        abortControllers.current[sid].abort()
        abortControllers.current[sid] = null
      }
      reconnectingRef.current[sid] = true
      reconnectStream(sid).finally(() => {
        reconnectingRef.current[sid] = false
      })
    }
  }

  // 起動時チェック
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { checkAndReconnect() }, [])

  // オフライン復帰時チェック
  useEffect(() => {
    const handle = () => checkAndReconnect(true)
    window.addEventListener('online', handle)
    return () => window.removeEventListener('online', handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // アプリ復帰時チェック
  useEffect(() => {
    let hiddenAt = null

    const onVisibility = () => {
      if (document.hidden) {
        hiddenAt = Date.now()
        return
      }
      const wasLong = hiddenAt != null && (Date.now() - hiddenAt) > 30_000
      hiddenAt = null

      // 復帰直後 1.5s は外部の reconnect 経路 (= buffer_length watcher) を抑止する。
      visibilitySuppressUntilRef.current = Date.now() + 1500

      for (const sid of allSessionIds()) buffer.cancelAndFlush(sid)
      if (wasLong) forceResyncAll()
      else checkAndReconnect(true)
      // 800ms 後の追加 checkAndReconnect は visibility 経路で既に走った reconnect と
      // 重複する race の温床だったので撤廃 (= 初回 checkAndReconnect で十分)。
      nextNextFrame(scrollToBottom)
    }

    const onPageShow = (e) => {
      if (e.persisted) forceResyncAll()
      else checkAndReconnect(true)
    }

    const onFocus = () => {
      if (!document.hidden) checkAndReconnect(true)
    }

    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pageshow', onPageShow)
    window.addEventListener('focus', onFocus)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pageshow', onPageShow)
      window.removeEventListener('focus', onFocus)
      // (A4 fix) reconnect 待ちのタイマも cleanup
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    reconnectingRef,
    reconnectStream,
    reconnectIfStreaming,
    checkAndReconnect,
    fetchLatest,
    forceResyncAll,
    eventDeps,
    visibilitySuppressUntilRef,
  }
}
