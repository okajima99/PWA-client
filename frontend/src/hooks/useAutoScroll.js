import { useState, useRef, useEffect, useCallback } from 'react'

// session_id をキーにした「タブ切替時の最下部固定 + 新着追従」 自動スクロール。
export function useAutoScroll({ messages, activeSession }) {
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [hasNew, setHasNew] = useState(false)
  const isAtBottomRef = useRef(true)
  const scrollerDomRef = useRef(null)
  const scrollThrottleRef = useRef(0)
  const msgLengthRef = useRef({})
  const programmaticScrollRef = useRef(false)
  const scrollEndTimerRef = useRef(null)
  const sid = activeSession?.id

  const scrollToBottom = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el) return
    programmaticScrollRef.current = true
    isAtBottomRef.current = true
    setHasNew(false)
    // scrollHeight は async (画像 / Markdown render) で変わるので、 数フレーム + 遅延
    // 設定で確実に最新まで飛ぶようにする
    el.scrollTo({ top: el.scrollHeight })
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (!scrollerDomRef.current) return
        scrollerDomRef.current.scrollTo({ top: scrollerDomRef.current.scrollHeight })
        setTimeout(() => {
          if (!scrollerDomRef.current) return
          scrollerDomRef.current.scrollTo({ top: scrollerDomRef.current.scrollHeight })
        }, 150)
      })
    })
    clearTimeout(scrollEndTimerRef.current)
    scrollEndTimerRef.current = setTimeout(() => {
      programmaticScrollRef.current = false
    }, 800)
  }, [])

  // 新着メッセージ時の自動スクロール
  useEffect(() => {
    if (!sid) return
    const cur = messages[sid] || []
    const currentLen = cur.length
    const prevLen = msgLengthRef.current[sid] || 0
    msgLengthRef.current[sid] = currentLen

    if (currentLen > prevLen) {
      if (isAtBottomRef.current) {
        requestAnimationFrame(() => { requestAnimationFrame(() => { scrollToBottom() }) })
      } else {
        setHasNew(true)
      }
    } else if (isAtBottomRef.current) {
      scrollToBottom()
    }
  }, [messages, sid, scrollToBottom])

  // タブ切替時は常に最下部
  useEffect(() => {
    if (!sid) return
    isAtBottomRef.current = true
    setShowScrollBtn(false)
    setHasNew(false)
    msgLengthRef.current[sid] = (messages[sid] || []).length

    const el = scrollerDomRef.current
    if (!el) return
    let cancelled = false
    let lastHeight = -1
    // 起動時 / タブ切替時は async load (IndexedDB 読み + 画像 render) で
    // scrollHeight が 5 秒以上遅れて確定するケースあり、 deadline を 5 秒に伸ばす
    const deadline = Date.now() + 5000
    const tick = () => {
      if (cancelled) return
      const cur = el.scrollHeight
      if (cur !== lastHeight && isAtBottomRef.current) {
        lastHeight = cur
        scrollToBottom()
      }
      if (Date.now() < deadline) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])

  // 画面回転時
  useEffect(() => {
    const onResize = () => {
      if (isAtBottomRef.current) scrollToBottom()
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [scrollToBottom])

  const onScroll = useCallback(() => {
    if (programmaticScrollRef.current) return
    const el = scrollerDomRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30
    isAtBottomRef.current = atBottom
    if (atBottom) setHasNew(false)
    const now = Date.now()
    if (now - scrollThrottleRef.current >= 150) {
      scrollThrottleRef.current = now
      setShowScrollBtn(!atBottom)
    }
  }, [])

  return {
    scrollerDomRef,
    isAtBottomRef,
    showScrollBtn,
    hasNew,
    scrollToBottom,
    onScroll,
  }
}
