import { useEffect, useRef } from 'react'
import { API_BASE, MAX_MESSAGES } from '../../constants.js'
import { generateId } from '../../utils/id.js'
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

  // reconnect: T1 移行で常に from=0 で全 buffer 再生する
  // - 204 なら false、データあり(ストリーミング完了)なら true を返す
  //
  // 注意点 (2026-05-17 fix):
  //   1. AbortController を必ず登録 (= 「最新を取得」 / 新 user POST で abort できる)
  //   2. cache-bust query を付ける (= iOS Safari の GET キャッシュバグ回避)
  //   3. cache: 'no-store' で念押し
  // sendMessage は POST + signal なので普通に動く、 reconnect は GET なので iOS が
  // 古いレスポンスを再利用してハングする報告あり。
  const reconnectStream = async (sid) => {
    const controller = new AbortController()
    abortControllers.current[sid] = controller
    const url = `${API_BASE}/chat/${sid}/reconnect?from=0&_t=${Date.now()}`
    const res = await fetch(url, { cache: 'no-store', signal: controller.signal })
    if (res.status === 204) return false
    if (!res.ok) return false

    isAtBottomRef.current = true
    // backend が現在進行中 (= state.complete=False = streaming=true) の時のみ loading=true 化。
    // forceResyncAll / visibility 復帰経由で「実は完了済みの buffer replay」 する場合に
    // 「一瞬 停止ボタン → すぐ送信ボタン」 の flicker が起きてたのを防ぐ。
    const statusNow = await fetch(`${API_BASE}/status/${sid}`)
      .then(r => r.ok ? r.json() : null).catch(() => null)
    const streamingNow = !!statusNow?.streaming
    if (streamingNow) {
      setLoading(prev => ({ ...prev, [sid]: true }))
    }
    setMessages(prev => {
      const cur = prev[sid] || []
      // 直前の send が失敗して error bubble が末尾に積まれているケースでは、
      // それを削除してから replay する。 そうしないと
      // [user] [error] [新 agent bubble] の順で表示されて UX が崩れる。
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

    buffer.resetBuf(sid)
    // replay は通常受信と同じロジック (uuid dedup) で済ませるので、 専用のフラグは不要

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let needsReconnect = false
    try {
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

      try {
        const s = await fetch(`${API_BASE}/status/${sid}`).then(r => r.json()).catch(() => null)
        if (s?.streaming) needsReconnect = true
      } catch { /* ignored */ }

      return true
    } finally {
      buffer.cancelAndFlush(sid)
      // setLoading(true) を冒頭で呼んでない場合は触らない (= visibility 復帰 flicker 防止)。
      // 呼んでる場合 (= streamingNow=true だった時) のみ false に戻して終了処理する。
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
      requestAnimationFrame(() => { scrollToBottom() })
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
      requestAnimationFrame(() => { requestAnimationFrame(() => { scrollToBottom() }) })
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
